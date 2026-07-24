from __future__ import annotations

import numpy as np
import pytest

import remote_dan.can_analysis as can_analysis
from remote_dan.can_analysis import (
    _parse_classic_frame,
    aggregate_can_identifiers,
    analyze_can_waveform,
    build_can_diagnostics,
    build_can_payload_heatmap,
    build_can_timeline,
    compare_can_decode_results,
    decode_can_waveform,
)


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


@pytest.mark.parametrize("reversed_polarity", (False, True))
def test_decode_finds_one_classical_frame_below_one_percent_dominant_occupancy(
    reversed_polarity: bool,
) -> None:
    frame = _classic_standard_frame(0x321, bytes.fromhex("1122334455667788"))
    time_us, can_h, can_l = _render_bus([frame], window_ms=25.0)
    if reversed_polarity:
        can_h, can_l = can_l, can_h

    decoded = decode_can_waveform(time_us, can_h, can_l)

    assert len(decoded["frames"]) == 1
    assert decoded["frames"][0]["identifier"] == 0x321
    assert decoded["polarity"] == ("reversed" if reversed_polarity else "expected")
    analyzed = analyze_can_waveform(time_us, can_h, can_l)
    assert analyzed["status"] == "analyzed"
    assert analyzed["validated_frame_count"] == 1
    assert analyzed["can_polarity"] == ("reversed" if reversed_polarity else "expected")


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


@pytest.mark.parametrize("reversed_polarity", (False, True))
def test_fd_only_decode_preserves_nominal_bitrate_and_candidate_count(
    reversed_polarity: bool,
) -> None:
    frames = [
        _fd_standard_frame(0x321, bytes.fromhex("1122334455667788")),
    ] * 6
    time_us, can_h, can_l = _render_bus(frames)
    if reversed_polarity:
        can_h, can_l = can_l, can_h

    decoded = decode_can_waveform(time_us, can_h, can_l)

    assert decoded["polarity"] == ("reversed" if reversed_polarity else "expected")
    assert decoded["nominal_bitrate_bps"] == 500_000
    assert decoded["unsupported_fd_candidate_count"] == 6


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


def test_v2_diagnostics_separate_physical_states_and_occupied_frame_traffic() -> None:
    frames = [
        _classic_standard_frame(0x100, b"\x10\x20"),
        _classic_standard_frame(0x200, b"\xAA\xBB"),
        _classic_standard_frame(0x100, b"\x10\x21"),
    ]
    time_us, can_h, can_l = _render_bus(frames, window_ms=2.0)
    decoded = decode_can_waveform(time_us, can_h, can_l)

    diagnostics = build_can_diagnostics(time_us, can_h, can_l, decoded)

    physical = diagnostics["physical_layer_diagnostics"]
    assert physical["capture_duration_us"] == pytest.approx(time_us[-1] - time_us[0])
    assert physical["sample_interval_us"] == pytest.approx(0.1)
    assert physical["selected_polarity"] == "expected"
    assert physical["state_method"] == "median recessive level and positive samples separated by at least 0.5 V"
    assert physical["dominant"]["can_h_v"]["median"] == pytest.approx(3.5)
    assert physical["dominant"]["can_l_v"]["median"] == pytest.approx(1.5)
    assert physical["dominant"]["differential_v"]["median"] == pytest.approx(2.0)
    assert physical["recessive"]["can_h_v"]["median"] == pytest.approx(2.5)
    assert physical["recessive"]["can_l_v"]["median"] == pytest.approx(2.5)
    assert physical["recessive"]["differential_v"]["median"] == pytest.approx(0.0)
    assert physical["dominant"]["common_mode_v"]["median"] == pytest.approx(2.5)
    assert physical["differential_span_v"] == pytest.approx(2.0)
    assert physical["transition_timing"]["rise_time_us"] is None
    assert physical["transition_timing"]["fall_time_us"] is None
    assert physical["transition_timing"]["available"] is False
    assert physical["transition_timing"]["edge_count"] > 0
    assert physical["transition_timing"]["sample_interval_us"] == pytest.approx(0.1)
    assert "termination" not in str(physical).lower()
    assert "healthy" not in str(physical).lower()

    integrity = diagnostics["integrity_diagnostics"]
    assert integrity["validated_frame_count"] == 3
    assert integrity["rejected_candidate_count"] == 0
    assert integrity["unsupported_fd_candidate_count"] == 0
    assert integrity["ack_dominant_count"] == 3
    assert integrity["ack_recessive_count"] == 0
    assert integrity["standard_frame_count"] == 3
    assert integrity["extended_frame_count"] == 0
    assert integrity["remote_frame_count"] == 0
    assert integrity["dlc_distribution"] == {"2": 3}
    assert integrity["observed_frame_count"] == 3
    assert integrity["observed_window_frame_rate_hz"] == pytest.approx(
        3 / (physical["capture_duration_us"] / 1_000_000)
    )
    assert integrity["observed_window_rate_numerator_frame_count"] == 3
    assert integrity["observed_window_rate_denominator_seconds"] == pytest.approx(
        physical["capture_duration_us"] / 1_000_000
    )
    assert "projected" in integrity["rate_qualification"]
    assert integrity["capture_boundary_censoring"] == {
        "leading_interval_censored": True,
        "trailing_interval_censored": True,
    }
    assert 0 < integrity["validated_classical_frame_wire_occupancy_percent"] < 100
    assert integrity["validated_classical_frame_wire_occupancy_method"] == (
        "union of complete validated Classical CAN frame source intervals divided by capture sample intervals"
    )
    assert integrity["validated_classical_frame_wire_occupancy_excludes"] == [
        "rejected or undecodable traffic",
        "CAN FD candidates",
        "frames clipped by either capture boundary",
        "intermission outside each complete validated frame interval",
    ]
    assert "frames_per_second" not in integrity
    assert "bus_occupancy_percent" not in integrity
    assert "bus_load_method" not in integrity
    assert "duration_qualified" not in integrity
    assert "confidence" not in integrity
    assert integrity["provenance"]["frame_authority"] == "CRC-valid Classical CAN frames"
    assert integrity["capabilities"]["long_listen_only_can_adapter_available"] is False
    assert integrity["capabilities"]["socketcan_or_provider_available"] is False
    assert integrity["capabilities"]["transmit_available"] is False


def test_identifier_v2_jitter_payload_percent_and_byte_bit_matrices_are_deterministic() -> None:
    frames = [
        {
            "identifier": 0x123,
            "identifier_hex": "0x123",
            "extended": False,
            "timestamp_us": timestamp,
            "remote": False,
            "dlc": 1,
            "payload_bytes": [payload],
            "payload_hex": f"{payload:02X}",
        }
        for timestamp, payload in (
            (0.0, 0x00), (10.0, 0x01), (30.0, 0x03), (60.0, 0x07)
        )
    ]

    summary = aggregate_can_identifiers(frames)[0]

    assert summary["first_timestamp_us"] == 0.0
    assert summary["last_timestamp_us"] == 60.0
    assert summary["interval_count"] == 3
    assert summary["mean_period_us"] == 20.0
    assert summary["median_interval_us"] == 20.0
    assert summary["inter_arrival_stddev_us"] == pytest.approx(8.164965809)
    assert summary["inter_arrival_stddev_measure"] == (
        "population standard deviation; reported only with at least 3 intervals"
    )
    assert summary["payload_state_change_count"] == 3
    assert summary["payload_state_change_percent"] == 100.0
    assert summary["byte_change_counts"] == [3]
    assert summary["bit_change_counts"] == [[0, 0, 0, 0, 0, 1, 1, 1]]

    one = aggregate_can_identifiers([frames[0]])[0]
    assert one["interval_count"] == 0
    assert one["mean_period_us"] is None
    assert one["median_interval_us"] is None
    assert one["inter_arrival_stddev_us"] is None
    assert one["payload_state_change_percent"] is None

    two = aggregate_can_identifiers(frames[:2])[0]
    assert two["interval_count"] == 1
    assert two["inter_arrival_stddev_us"] is None


def test_payload_aggregation_separates_dlc_rtr_and_comparable_byte_changes() -> None:
    states = [
        (0.0, False, 1, [0xAA]),
        (10.0, False, 2, [0xAA, 0xBB]),
        (20.0, False, 1, [0xAB]),
        (30.0, True, 1, []),
        (40.0, False, 1, [0xAB]),
    ]
    frames = [{
        "identifier": 0x123,
        "identifier_hex": "0x123",
        "extended": False,
        "timestamp_us": timestamp,
        "remote": remote,
        "dlc": dlc,
        "payload_bytes": payload,
        "payload_hex": bytes(payload).hex().upper(),
    } for timestamp, remote, dlc, payload in states]

    summary = aggregate_can_identifiers(frames)[0]

    assert summary["payload_state_transition_count"] == 4
    assert summary["payload_state_change_count"] == 4
    assert summary["dlc_transition_count"] == 2
    assert summary["rtr_data_transition_count"] == 2
    assert summary["comparable_payload_transition_count"] == 2
    assert summary["comparable_payload_change_count"] == 1
    assert summary["introduced_byte_count"] == 1
    assert summary["removed_byte_count"] == 1
    assert summary["byte_change_counts"] == [1, 0]
    assert summary["bit_change_counts"] == [
        [0, 0, 0, 0, 0, 0, 0, 1],
        [0, 0, 0, 0, 0, 0, 0, 0],
    ]
    assert "payload_change_count" not in summary
    assert "payload_change_percent" not in summary


def test_timeline_is_bounded_chronological_and_payload_bounded() -> None:
    frames = [
        {
            "timestamp_us": float(index),
            "identifier": index % 0x7FF,
            "identifier_hex": f"0x{index % 0x7FF:03X}",
            "extended": False,
            "remote": False,
            "dlc": 1,
            "payload_hex": f"{index % 256:02X}",
            "ack_slot": "dominant",
        }
        for index in reversed(range(205))
    ]

    timeline = build_can_timeline(frames, limit=200)

    assert len(timeline) == 200
    assert [item["timestamp_us"] for item in timeline] == list(map(float, range(200)))
    assert set(timeline[0]) == {
        "timestamp_us", "identifier", "identifier_hex", "extended", "remote",
        "dlc", "payload_hex", "ack_slot",
    }
    with pytest.raises(ValueError, match="timeline limit"):
        build_can_timeline(frames, limit=201)


def test_payload_heatmap_is_fixed_bounded_ranked_and_filters_before_limiting() -> None:
    identifiers = [{
        "identifier": index,
        "identifier_hex": f"0x{index:03X}",
        "extended": False,
        "frame_count": index + 1,
        "payload_state_change_count": index,
        "bit_change_counts": [[index] * 8],
        "introduced_byte_count": 0,
        "removed_byte_count": 0,
    } for index in range(202)]
    identifiers.append({
        "identifier": 0x100,
        "identifier_hex": "0x00000100",
        "extended": True,
        "frame_count": 999,
        "payload_state_change_count": 999,
        "bit_change_counts": [[9] * 8],
        "introduced_byte_count": 1,
        "removed_byte_count": 2,
    })

    heatmap = build_can_payload_heatmap(
        identifiers,
        capture_duration_us=25_000.0,
    )

    assert heatmap["source_identifier_count"] == 203
    assert heatmap["total_identifier_count"] == 203
    assert heatmap["returned_identifier_count"] == 200
    assert heatmap["identifiers_truncated"] is True
    assert heatmap["identifier_limit"] == 200
    assert heatmap["bin_count"] == 64
    assert heatmap["bin_width_bits"] == 1
    assert heatmap["cell_limit"] == 12_800
    assert heatmap["returned_cell_count"] == 12_800
    assert heatmap["capture_interval_us"] == {"start": 0.0, "end": 25_000.0}
    assert heatmap["ranking_policy"] == (
        "payload-state changes descending, frame count descending, identifier ascending, standard before extended"
    )
    assert "both adjacent frames are data" in heatmap["cell_semantics"]
    assert heatmap["identifiers"][0]["key"] == {
        "identifier": 0x100, "extended": True,
    }
    assert len(heatmap["identifiers"][0]["cells"]) == 64

    filtered = build_can_payload_heatmap(
        identifiers,
        capture_duration_us=25_000.0,
        identifier_filter="00000100",
    )
    assert filtered["total_identifier_count"] == 1
    assert filtered["returned_identifier_count"] == 1
    assert filtered["identifiers"][0]["key"]["extended"] is True


def test_comparison_is_bounded_deterministic_and_reports_comparable_deltas() -> None:
    def identifier(identifier: int, count: int, period: float, change: float) -> dict[str, object]:
        return {
            "identifier": identifier,
            "identifier_hex": f"0x{identifier:03X}",
            "extended": False,
            "frame_count": count,
            "mean_frequency_hz": 1_000_000.0 / period,
            "mean_period_us": period,
            "payload_state_change_percent": change,
        }

    common = {
        "artifact_schema_version": 2,
        "decoder_algorithm_version": 2,
        "analyzer_version": 2,
        "decoder_settings": {"classical_can_only": True, "max_source_samples": 1_000_000},
        "nominal_bitrate_bps": 500_000,
        "can_polarity": "expected",
    }
    baseline = {
        **common,
        "run_id": "baseline-run",
        "capture_id": 10,
        "captured_at": "2026-07-23T12:01:00+00:00",
        "source_run_id": "source-a",
        "source_capture_id": 1,
        "source_captured_at": "2026-07-23T12:00:00+00:00",
        "source_sha256": "a" * 64,
        "source_manifest_sha256": "b" * 64,
        "identifiers": [identifier(0x100, 4, 1000.0, 25.0), identifier(0x200, 2, 2000.0, 0.0)],
        "physical_layer_diagnostics": {
            "capture_duration_us": 10_000.0,
            "sample_interval_us": 0.1,
            "differential_span_v": 2.0,
            "dominant": {"can_h_v": {"median": 3.5}},
        },
        "integrity_diagnostics": {
            "observed_window_frame_rate_hz": 600.0,
            "validated_classical_frame_wire_occupancy_percent": 20.0,
            "validated_frame_count": 6,
        },
    }
    candidate = {
        **common,
        "run_id": "candidate-run",
        "capture_id": 11,
        "captured_at": "2026-07-23T12:02:00+00:00",
        "source_run_id": "source-b",
        "source_capture_id": 2,
        "source_captured_at": "2026-07-23T12:00:30+00:00",
        "source_sha256": "c" * 64,
        "source_manifest_sha256": "d" * 64,
        "identifiers": [identifier(0x100, 6, 800.0, 50.0), identifier(0x300, 1, 3000.0, 0.0)],
        "physical_layer_diagnostics": {
            "capture_duration_us": 12_000.0,
            "sample_interval_us": 0.1,
            "differential_span_v": 1.8,
            "dominant": {"can_h_v": {"median": 3.4}},
        },
        "integrity_diagnostics": {
            "observed_window_frame_rate_hz": 700.0,
            "validated_classical_frame_wire_occupancy_percent": 25.0,
            "validated_frame_count": 7,
        },
    }

    comparison = compare_can_decode_results(baseline, candidate)

    assert comparison["artifact_schema_version"] == 2
    assert comparison["provenance"]["baseline"] == {
        "run_id": "baseline-run",
        "capture_id": 10,
        "captured_at": "2026-07-23T12:01:00+00:00",
        "source_run_id": "source-a",
        "source_capture_id": 1,
        "source_captured_at": "2026-07-23T12:00:00+00:00",
        "source_sha256": "a" * 64,
        "source_manifest_sha256": "b" * 64,
    }
    assert comparison["provenance"]["candidate"]["run_id"] == "candidate-run"
    assert comparison["provenance"]["authority"] == "complete authoritative can_decode child chains"
    assert comparison["same_source_warning"] is None
    assert comparison["compatibility"]["duration"]["equal"] is False
    assert "observed-window rates only" in comparison["compatibility"]["duration"]["qualification"]
    assert comparison["limits"]["identifier_delta_limit"] == 200
    assert comparison["observed_only_in_candidate"] == [{
        "identifier": 0x300, "identifier_hex": "0x300", "extended": False,
    }]
    assert comparison["observed_only_in_baseline"] == [{
        "identifier": 0x200, "identifier_hex": "0x200", "extended": False,
    }]
    assert comparison["common_identifiers"] == [{
        "identifier": 0x100, "identifier_hex": "0x100", "extended": False,
    }]
    delta = comparison["identifier_deltas"][0]
    assert delta["baseline_observed_window_rate_hz"] == 400.0
    assert delta["candidate_observed_window_rate_hz"] == 500.0
    assert delta["observed_window_rate_hz_delta"] == 100.0
    assert "frame_count_delta" not in delta
    assert delta["mean_period_us_delta"] == -200.0
    assert delta["payload_state_change_percent_delta"] == 25.0
    assert comparison["physical_deltas"]["differential_span_v"] == pytest.approx(-0.2)
    assert comparison["integrity_deltas"]["validated_classical_frame_wire_occupancy_percent"] == 5.0

    with pytest.raises(ValueError, match="different runs"):
        compare_can_decode_results(baseline, baseline)

    opposite_polarity = {**candidate, "can_polarity": "reversed"}
    with pytest.raises(ValueError, match="polarity"):
        compare_can_decode_results(baseline, opposite_polarity)
    with pytest.raises(ValueError, match="identifier limit"):
        compare_can_decode_results(baseline, candidate, identifier_limit=True)
    zero_duration = {
        **candidate,
        "physical_layer_diagnostics": {
            **candidate["physical_layer_diagnostics"],
            "capture_duration_us": 0.0,
        },
    }
    with pytest.raises(ValueError, match="capture duration"):
        compare_can_decode_results(baseline, zero_duration)
    incompatible = dict(candidate, artifact_schema_version=1)
    with pytest.raises(ValueError, match="artifact schema"):
        compare_can_decode_results(baseline, incompatible)
    oversized = dict(candidate, identifiers=[
        identifier(0x100, 1, 1.0, 0.0),
        *[identifier(index, 1, 1.0, 0.0) for index in range(201)],
    ])
    bounded = compare_can_decode_results(baseline, oversized)
    assert bounded["identifier_delta_total_count"] == 1
    assert bounded["identifier_delta_returned_count"] == 1
    assert bounded["identifier_deltas_truncated"] is False
    assert bounded["observed_only_in_candidate_total_count"] == 201
    assert bounded["observed_only_in_candidate_returned_count"] == 200
    assert bounded["observed_only_in_candidate_truncated"] is True
