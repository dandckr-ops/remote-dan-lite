from __future__ import annotations

import numpy as np
import pytest

import remote_dan.can_analysis as can_analysis
from remote_dan.can_analysis import _parse_classic_frame, analyze_can_waveform


def _bits(value: int, width: int) -> list[int]:
    return [int(bit) for bit in f"{value:0{width}b}"]


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


def _can_crc15(bits: list[int]) -> list[int]:
    crc = 0
    for bit in bits:
        feedback = bit ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if feedback:
            crc ^= 0x4599
    return _bits(crc, 15)


def _classic_extended_frame(
    identifier: int,
    data: bytes,
    *,
    r1: int = 0,
) -> list[int]:
    identifier_a = identifier >> 18
    identifier_b = identifier & ((1 << 18) - 1)
    header_and_data = (
        [0]
        + _bits(identifier_a, 11)
        + [1, 1]
        + _bits(identifier_b, 18)
        + [0, r1, 0]
        + _bits(len(data), 4)
        + [bit for value in data for bit in _bits(value, 8)]
    )
    crc = _can_crc15(header_and_data)
    return _stuff(header_and_data + crc) + [1, 0, 1] + [1] * 7 + [1] * 3


def _classic_standard_frame(identifier: int, data: bytes) -> list[int]:
    header_and_data = (
        [0]
        + _bits(identifier, 11)
        + [0, 0, 0]
        + _bits(len(data), 4)
        + [bit for value in data for bit in _bits(value, 8)]
    )
    crc = _can_crc15(header_and_data)
    return _stuff(header_and_data + crc) + [1, 0, 1] + [1] * 7 + [1] * 3


def _terminal_crc_stuff_frame() -> tuple[list[int], int]:
    for identifier in range(0x800):
        body = [0] + _bits(identifier, 11) + [0, 0, 0] + _bits(0, 4)
        stuffed = _stuff(body + _can_crc15(body))
        if len(stuffed) >= 5 and len(set(stuffed[-5:])) == 1:
            terminal_stuff = 1 - stuffed[-1]
            return (
                stuffed + [terminal_stuff, 1, 0, 1] + [1] * 7 + [1] * 3,
                identifier,
            )
    raise AssertionError("failed to synthesize terminal CRC stuff case")


def test_classic_parser_consumes_and_validates_terminal_crc_stuff_bit() -> None:
    frame, identifier = _terminal_crc_stuff_frame()

    decoded = _parse_classic_frame(frame)

    assert decoded is not None
    assert decoded["identifier"] == identifier
    terminal_stuff_index = int(decoded["complete_raw_bit_count"]) - 11
    assert _parse_classic_frame(frame[:terminal_stuff_index] + frame[terminal_stuff_index + 1:]) is None
    malformed = list(frame)
    malformed[terminal_stuff_index] = 1 - malformed[terminal_stuff_index]
    assert _parse_classic_frame(malformed) is None


@pytest.mark.parametrize("r1, valid", [(0, True), (1, False)])
def test_classic_extended_parser_requires_dominant_r1(r1: int, valid: bool) -> None:
    decoded = _parse_classic_frame(
        _classic_extended_frame(0x18F00401, b"\x11\x22", r1=r1)
    )

    assert (decoded is not None) is valid


@pytest.mark.parametrize(
    ("expected", "reversed_result", "selected"),
    [
        ((4, 1), (2, 0), "expected"),
        ((1, 0), (5, 2), "reversed"),
        ((3, 1), (3, 1), "expected"),
        ((0, 0), (0, 0), "expected"),
    ],
)
def test_decode_selects_stronger_complete_frame_orientation(
    monkeypatch: pytest.MonkeyPatch,
    expected: tuple[int, int],
    reversed_result: tuple[int, int],
    selected: str,
) -> None:
    calls = iter((expected, reversed_result))

    def fake_decode(*_args: object) -> dict[str, object]:
        frame_count, rejected_count = next(calls)
        return {
            "frames": [{"identifier": index} for index in range(frame_count)],
            "rejected_candidate_count": rejected_count,
        }

    monkeypatch.setattr(can_analysis, "_decode_can_orientation", fake_decode)
    samples = np.asarray([0.0, 0.1, 0.2])

    decoded = can_analysis.decode_can_waveform(samples, samples, samples)

    assert decoded["polarity"] == selected
    assert len(decoded["frames"]) == (
        expected[0] if selected == "expected" else reversed_result[0]
    )
    assert bool(decoded["warnings"]) is (selected == "reversed")


def _fd_standard_frame(identifier: int, data: bytes, *, brs: bool = False) -> list[int]:
    header_and_data = (
        [0]
        + _bits(identifier, 11)
        + [0, 0, 1, 0, int(brs), 0]
        + _bits(len(data), 4)
        + [bit for value in data for bit in _bits(value, 8)]
    )
    crc = [index % 2 for index in range(17)]
    return _stuff(header_and_data + crc) + [1, 0, 1] + [1] * 7 + [1] * 3


def _stuff_tagged(bits: list[tuple[int, str]]) -> list[tuple[int, str]]:
    wire: list[tuple[int, str]] = []
    previous: int | None = None
    run = 0
    for bit, phase in bits:
        if previous is not None and run == 5:
            stuff = 1 - previous
            wire.append((stuff, phase))
            previous = stuff
            run = 1
        wire.append((bit, phase))
        if bit == previous:
            run += 1
        else:
            previous = bit
            run = 1
    return wire


def _fd_brs_frame(identifier: int, data: bytes) -> list[tuple[int, str]]:
    nominal = [0] + _bits(identifier, 11) + [0, 0, 1, 0, 1]
    data_phase = (
        [0]
        + _bits(len(data), 4)
        + [bit for value in data for bit in _bits(value, 8)]
        + [index % 2 for index in range(17)]
    )
    stuffed = _stuff_tagged(
        [(bit, "nominal") for bit in nominal]
        + [(bit, "data") for bit in data_phase]
    )
    return stuffed + [
        (bit, "nominal") for bit in ([1, 0, 1] + [1] * 7 + [1] * 3)
    ]


def _render_bus(
    frames: list[list[int]],
    *,
    bitrate_bps: int = 500_000,
    sample_interval_us: float = 0.1,
    window_ms: float = 25.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    samples_per_bit = round((1_000_000 / bitrate_bps) / sample_interval_us)
    wire_bits: list[int] = [1] * 20
    for frame in frames:
        wire_bits.extend(frame)
        wire_bits.extend([1] * 20)
    wire = np.repeat(np.array(wire_bits, dtype=np.int8), samples_per_bit)
    sample_count = round(window_ms * 1000 / sample_interval_us)
    if wire.size < sample_count:
        wire = np.pad(wire, (0, sample_count - wire.size), constant_values=1)
    wire = wire[:sample_count]
    dominant = (wire == 0).astype(np.float64)
    time_us = np.arange(sample_count, dtype=np.float64) * sample_interval_us
    can_h = 2.5 + dominant
    can_l = 2.5 - dominant
    return time_us, can_h, can_l


def _render_brs_bus(
    frames: list[list[tuple[int, str]]],
    *,
    nominal_bitrate_bps: int = 500_000,
    data_bitrate_bps: int = 2_000_000,
    sample_interval_us: float = 0.05,
    window_ms: float = 25.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    nominal_samples = round((1_000_000 / nominal_bitrate_bps) / sample_interval_us)
    samples: list[int] = [1] * (20 * nominal_samples)
    for frame in frames:
        for bit, phase in frame:
            bitrate = nominal_bitrate_bps if phase == "nominal" else data_bitrate_bps
            samples.extend([bit] * round((1_000_000 / bitrate) / sample_interval_us))
        samples.extend([1] * (20 * nominal_samples))
    sample_count = round(window_ms * 1000 / sample_interval_us)
    wire = np.asarray(samples[:sample_count], dtype=np.int8)
    if wire.size < sample_count:
        wire = np.pad(wire, (0, sample_count - wire.size), constant_values=1)
    dominant = (wire == 0).astype(np.float64)
    time_us = np.arange(sample_count, dtype=np.float64) * sample_interval_us
    return time_us, 2.5 + dominant, 2.5 - dominant


def test_analysis_identifies_classic_j1939_speed_and_load() -> None:
    eec1 = 0x18F00401  # PGN 61444
    ccvs1 = 0x18FEF100  # PGN 65265
    frames = [
        _classic_extended_frame(eec1, bytes.fromhex("1122334455667788")),
        _classic_extended_frame(ccvs1, bytes.fromhex("8877665544332211")),
    ] * 4
    time_us, can_h, can_l = _render_bus(frames)

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["status"] == "analyzed"
    assert result["physical_layer"] == "High-speed CAN-family (ISO 11898-2)"
    assert result["nominal_bitrate_bps"] == 500_000
    assert result["samples_per_nominal_bit"] == 20.0
    assert result["frame_count"] == 8
    assert result["crc_valid_header_count"] == 8
    assert 5.0 < result["bus_load_percent"] < 15.0
    assert result["identifier_format"] == "29-bit"
    assert result["bus_type"] == "Classical CAN"
    assert result["protocol"]["name"] == "SAE J1939"
    assert result["protocol"]["confidence"] == "high"
    assert result["protocol"]["known_pgns"] == [61444, 65265]


def test_analysis_refuses_to_guess_from_under_sampled_waveform() -> None:
    frame = _classic_extended_frame(
        0x18F00401,
        bytes.fromhex("1122334455667788"),
    )
    time_us, can_h, can_l = _render_bus(
        [frame] * 4,
        sample_interval_us=2.0,
    )

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["status"] == "insufficient_timing_evidence"
    assert result["physical_layer"] == "CAN analysis resolution not met"
    assert result["sample_interval_us"] == 2.0
    assert "CAN Analyze window" in result["warnings"][0]
    assert "nominal_bitrate_bps" not in result


def test_analysis_fingerprints_obd_iso_tp_standard_ids() -> None:
    frames = [
        _classic_standard_frame(0x7DF, bytes.fromhex("02010C0000000000")),
        _classic_standard_frame(0x7E8, bytes.fromhex("04410C1AF8000000")),
    ] * 4
    time_us, can_h, can_l = _render_bus(frames)

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["status"] == "analyzed"
    assert result["identifier_format"] == "11-bit"
    assert result["protocol"]["name"] == "ISO-TP / OBD-II diagnostic traffic"
    assert result["protocol"]["confidence"] == "high"
    assert "0x7DF" in result["protocol"]["evidence"]


def test_analysis_identifies_can_fd_without_bitrate_switching() -> None:
    frames = [
        _fd_standard_frame(0x321, bytes.fromhex("1122334455667788")),
    ] * 6
    time_us, can_h, can_l = _render_bus(frames)

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["status"] == "analyzed"
    assert result["bus_type"] == "CAN FD"
    assert result["nominal_bitrate_bps"] == 500_000
    assert result["data_bitrate_bps"] is None
    assert result["fd_brs_observed"] is False
    assert result["identifier_format"] == "11-bit"


def test_analysis_separates_can_fd_nominal_and_brs_data_rates() -> None:
    frames = [
        _fd_brs_frame(0x321, bytes.fromhex("1122334455667788")),
    ] * 8
    time_us, can_h, can_l = _render_brs_bus(frames)

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["status"] == "analyzed"
    assert result["bus_type"] == "CAN FD"
    assert result["fd_brs_observed"] is True
    assert result["nominal_bitrate_bps"] == 500_000
    assert result["data_bitrate_bps"] == 2_000_000
    assert result["samples_per_nominal_bit"] == 40.0


def test_analysis_fingerprints_nmea_2000_known_pgns() -> None:
    # Priority 2, data page 1, PGNs 127250 and 127488, source 0x23.
    frames = [
        _classic_extended_frame(0x09F11223, bytes.fromhex("1122334455667788")),
        _classic_extended_frame(0x09F20023, bytes.fromhex("8877665544332211")),
    ] * 4
    time_us, can_h, can_l = _render_bus(frames, bitrate_bps=250_000)

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["protocol"]["name"] == "NMEA 2000"
    assert result["protocol"]["confidence"] == "high"
    assert result["protocol"]["known_pgns"] == [127250, 127488]


def test_analysis_fingerprints_canopen_heartbeat_and_sdo_ids() -> None:
    frames = [
        _classic_standard_frame(0x701, bytes.fromhex("0500000000000000")),
        _classic_standard_frame(0x601, bytes.fromhex("4000100000000000")),
        _classic_standard_frame(0x581, bytes.fromhex("4300100011223344")),
    ] * 3
    time_us, can_h, can_l = _render_bus(frames, bitrate_bps=250_000)

    result = analyze_can_waveform(time_us, can_h, can_l)

    assert result["protocol"]["name"] == "CANopen"
    assert result["protocol"]["confidence"] == "high"
    assert "Heartbeat 0x701" in result["protocol"]["evidence"]
