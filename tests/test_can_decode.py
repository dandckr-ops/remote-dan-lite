from __future__ import annotations

import numpy as np

from remote_dan.can_analysis import (
    aggregate_can_identifiers,
    analyze_can_waveform,
    decode_can_waveform,
)


def _bits(value: int, width: int) -> list[int]:
    return [int(bit) for bit in f"{value:0{width}b}"]


def _crc15(bits: list[int]) -> list[int]:
    crc = 0
    for bit in bits:
        feedback = bit ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if feedback:
            crc ^= 0x4599
    return _bits(crc, 15)


def _stuff(bits: list[int]) -> list[int]:
    wire: list[int] = []
    previous: int | None = None
    run = 0
    for bit in bits:
        if previous is not None and run == 5:
            stuff = 1 - previous
            wire.append(stuff)
            previous = stuff
            run = 1
        wire.append(bit)
        if bit == previous:
            run += 1
        else:
            previous = bit
            run = 1
    return wire


def _classic_frame(
    identifier: int,
    payload: bytes,
    *,
    extended: bool = False,
    remote: bool = False,
    dlc: int | None = None,
    corrupt_crc: bool = False,
) -> list[int]:
    raw_dlc = len(payload) if dlc is None else dlc
    if extended:
        identifier_a = identifier >> 18
        identifier_b = identifier & ((1 << 18) - 1)
        body = (
            [0]
            + _bits(identifier_a, 11)
            + [1, 1]
            + _bits(identifier_b, 18)
            + [1 if remote else 0, 0, 0]
            + _bits(raw_dlc, 4)
        )
    else:
        body = [0] + _bits(identifier, 11) + [1 if remote else 0, 0, 0] + _bits(raw_dlc, 4)
    if not remote:
        body += [bit for value in payload[:8] for bit in _bits(value, 8)]
    crc = _crc15(body)
    if corrupt_crc:
        crc[-1] ^= 1
    return _stuff(body + crc) + [1, 0, 1] + [1] * 7 + [1] * 3


def _render(
    entries: list[tuple[int, list[int]]],
    *,
    bitrate_bps: int = 500_000,
    sample_interval_us: float = 0.1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    samples_per_bit = round((1_000_000 / bitrate_bps) / sample_interval_us)
    wire: list[int] = [1] * 20
    cursor = 20
    for idle_bits, frame in entries:
        wire.extend([1] * idle_bits)
        cursor += idle_bits
        wire.extend(frame)
        cursor += len(frame)
    wire.extend([1] * 20)
    logical = np.repeat(np.asarray(wire, dtype=np.int8), samples_per_bit)
    dominant = (logical == 0).astype(np.float64)
    time_us = np.arange(logical.size, dtype=np.float64) * sample_interval_us
    return time_us, 2.5 + dominant, 2.5 - dominant


def _fd_candidate(identifier: int) -> list[int]:
    header = [0] + _bits(identifier, 11) + [0, 0, 1, 0, 0]
    return _stuff(header + [0, 1] * 12) + [1, 0, 1] + [1] * 10


def test_decodes_standard_classical_frame_payload_crc_and_timestamp() -> None:
    frame = _classic_frame(0x321, bytes.fromhex("11223344"))
    time_us, can_h, can_l = _render([(30, frame)])

    result = decode_can_waveform(time_us, can_h, can_l)

    assert result["polarity"] == "expected"
    assert result["nominal_bitrate_bps"] == 500_000
    assert result["rejected_candidate_count"] == 0
    assert len(result["frames"]) == 1
    decoded = result["frames"][0]
    assert decoded["timestamp_us"] == 100.0
    assert decoded["identifier"] == 0x321
    assert decoded["identifier_hex"] == "0x321"
    assert decoded["extended"] is False
    assert decoded["remote"] is False
    assert decoded["dlc"] == 4
    assert decoded["payload_bytes"] == [0x11, 0x22, 0x33, 0x44]
    assert decoded["payload_hex"] == "11223344"
    assert decoded["crc_valid"] is True
    assert decoded["nominal_bitrate_bps"] == 500_000
    assert decoded["source_sample_start"] < decoded["source_sample_end"]


def test_decodes_extended_classical_frame() -> None:
    frame = _classic_frame(0x18F00401, bytes.fromhex("AABBCCDDEEFF0011"), extended=True)
    result = decode_can_waveform(*_render([(0, frame)]))

    assert len(result["frames"]) == 1
    decoded = result["frames"][0]
    assert decoded["identifier"] == 0x18F00401
    assert decoded["identifier_hex"] == "0x18F00401"
    assert decoded["extended"] is True
    assert decoded["dlc"] == 8
    assert decoded["payload_hex"] == "AABBCCDDEEFF0011"


def test_retries_reversed_source_orientation_without_relabeling_source() -> None:
    time_us, can_h, can_l = _render([(0, _classic_frame(0x123, b"\x01\x02"))])

    result = decode_can_waveform(time_us, can_l, can_h)

    assert result["polarity"] == "reversed"
    assert [frame["identifier"] for frame in result["frames"]] == [0x123]
    assert "source labels" in result["warnings"][0]


def test_crc_invalid_and_incomplete_candidates_are_rejected_not_published() -> None:
    valid = _classic_frame(0x100, b"\xAA")
    invalid = _classic_frame(0x200, b"\xBB", corrupt_crc=True)
    incomplete = _classic_frame(0x300, b"\xCC\xDD")[:25]

    result = decode_can_waveform(*_render([(0, valid), (20, invalid), (20, incomplete)]))

    assert [frame["identifier"] for frame in result["frames"]] == [0x100]
    assert result["rejected_candidate_count"] == 2
    assert all(frame["crc_valid"] is True for frame in result["frames"])


def test_classical_structure_raw_dlc_rtr_and_complete_bounds() -> None:
    frames = [
        _classic_frame(0x001, b"", dlc=0),
        _classic_frame(0x002, bytes(range(8)), dlc=15),
        _classic_frame(0x003, b"", remote=True, dlc=8),
    ]
    time_us, can_h, can_l = _render([(10, item) for item in frames])
    time_us += 7_500.0

    result = decode_can_waveform(time_us, can_h, can_l)

    assert [(item["dlc"], item["payload_hex"], item["remote"]) for item in result["frames"]] == [
        (0, "", False),
        (15, "0001020304050607", False),
        (8, "", True),
    ]
    assert result["frames"][0]["timestamp_us"] == 60.0
    assert all(item["source_sample_end"] > item["source_sample_start"] for item in result["frames"])


def test_corrupt_classical_trailer_and_post_crc_truncation_are_rejected() -> None:
    base = _classic_frame(0x321, b"\x12\x34")
    variants: list[list[int]] = []
    trailer_start = len(base) - 13
    for index in range(5, trailer_start):
        if len(set(base[index - 5:index])) == 1 and base[index] != base[index - 1]:
            damaged_stuff = base.copy()
            damaged_stuff[index] = base[index - 1]
            variants.append(damaged_stuff)
            break
    for trailer_offset in (0, 2, 3):
        damaged = base.copy()
        damaged[trailer_start + trailer_offset] = 0
        variants.append(damaged)
    for damaged in variants:
        result = decode_can_waveform(*_render([(0, damaged)]))
        assert result["frames"] == []
        assert result["rejected_candidate_count"] >= 1

    truncated = base[: len(base) - 13]
    time_us, can_h, can_l = _render([(0, truncated)])
    trailing_idle_samples = 20 * 20
    result = decode_can_waveform(
        time_us[:-trailing_idle_samples],
        can_h[:-trailing_idle_samples],
        can_l[:-trailing_idle_samples],
    )
    assert result["frames"] == []
    assert result["rejected_candidate_count"] >= 1


def test_mixed_classical_and_fd_publishes_only_validated_classical_rows() -> None:
    result = decode_can_waveform(*_render([
        (0, _classic_frame(0x123, b"\xAA")),
        (20, _fd_candidate(0x456)),
    ]))

    assert [frame["identifier"] for frame in result["frames"]] == [0x123]
    assert result["unsupported_fd_candidate_count"] == 1
    assert result["rejected_candidate_count"] == 1


def test_identifier_aggregation_is_deterministic_and_counts_payload_changes() -> None:
    def frame(identifier: int, timestamp_us: float, payload_hex: str) -> dict[str, object]:
        return {
            "identifier": identifier,
            "identifier_hex": f"0x{identifier:03X}",
            "extended": False,
            "timestamp_us": timestamp_us,
            "payload_hex": payload_hex,
            "payload_bytes": list(bytes.fromhex(payload_hex)),
        }

    summaries = aggregate_can_identifiers([
        frame(0x200, 250.0, "FF"),
        frame(0x100, 600.0, "BB01"),
        frame(0x100, 100.0, "AA00"),
        frame(0x100, 300.0, "AA01"),
    ])

    assert [item["identifier"] for item in summaries] == [0x100, 0x200]
    first = summaries[0]
    assert first["frame_count"] == 3
    assert first["first_timestamp_us"] == 100.0
    assert first["last_timestamp_us"] == 600.0
    assert first["observed_duration_us"] == 500.0
    assert first["mean_period_us"] == 250.0
    assert first["mean_frequency_hz"] == 4000.0
    assert first["min_interval_us"] == 200.0
    assert first["max_interval_us"] == 300.0
    assert first["payload_change_count"] == 2
    assert first["last_payload_hex"] == "BB01"
    assert first["byte_change_counts"] == [1, 1]
    assert summaries[1]["mean_period_us"] is None
    assert summaries[1]["mean_frequency_hz"] is None
    assert summaries[1]["min_interval_us"] is None


def test_aggregate_analysis_adds_validated_frames_for_supplied_orientation() -> None:
    time_us, can_h, can_l = _render([(0, _classic_frame(0x456, b"\x10\x20"))])

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["status"] == "analyzed"
    assert result["can_polarity"] == "expected"
    assert result["frames"][0]["identifier"] == 0x456
    assert result["identifiers"][0]["last_payload_hex"] == "1020"
    assert result["rejected_candidate_count"] == 0


def test_fixed_independent_wire_vectors_cover_terminal_stuff_ack_and_extended_r1() -> None:
    # Fixed independent-reviewer reproduction vectors. These are raw Classical CAN
    # wire bits, not generated at runtime and make no OEM/DBC claim.
    dominant_ack = "00000100000100000100010001110100000100111000001101111111111"
    recessive_ack = "00000100000100000100010001110100000100111000001111111111111"
    malformed_extended_r1 = (
        "0110001111001100000100100000100001010000011101000110001101011111111111"
    )

    for vector in (dominant_ack, recessive_ack):
        raw = [int(bit) for bit in vector]
        result = decode_can_waveform(*_render([(0, raw), (20, raw), (20, raw)]))
        assert {
            (frame["identifier"], frame["payload_hex"]) for frame in result["frames"]
        } == {(0x000, "1D")}

    rejected = decode_can_waveform(
        *_render([(0, [int(bit) for bit in malformed_extended_r1])])
    )
    assert rejected["frames"] == []
    assert rejected["rejected_candidate_count"] >= 1
