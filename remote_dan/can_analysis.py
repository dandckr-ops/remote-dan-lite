from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np


NOMINAL_BITRATES = (
    10_000,
    20_000,
    33_333,
    50_000,
    83_333,
    100_000,
    125_000,
    250_000,
    500_000,
    800_000,
    1_000_000,
)
DATA_BITRATES = (2_000_000, 4_000_000, 5_000_000, 8_000_000)
TIMING_BITRATES = NOMINAL_BITRATES + DATA_BITRATES

KNOWN_J1939_PGNS = {
    59_904: "Request",
    60_928: "Address Claimed",
    61_444: "Electronic Engine Controller 1",
    65_226: "Active Diagnostic Trouble Codes",
    65_265: "Cruise Control / Vehicle Speed 1",
    65_270: "Inlet / Exhaust Conditions 1",
}
KNOWN_NMEA2000_PGNS = {
    126_992: "System Time",
    127_250: "Vessel Heading",
    127_488: "Engine Parameters, Rapid Update",
    129_025: "Position, Rapid Update",
}


def _integer(bits: list[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def can_crc15_bits(bits: list[int]) -> list[int]:
    crc = 0
    for bit in bits:
        feedback = int(bit) ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if feedback:
            crc ^= 0x4599
    return [int(bit) for bit in f"{crc:015b}"]


def _destuff(raw_bits: list[int], *, max_output_bits: int) -> tuple[list[int], bool]:
    output: list[int] = []
    previous: int | None = None
    run = 0
    for bit in raw_bits:
        if previous is not None and run == 5:
            if bit == previous:
                return output, False
            previous = bit
            run = 1
            continue
        output.append(bit)
        if len(output) >= max_output_bits:
            return output, True
        if bit == previous:
            run += 1
        else:
            previous = bit
            run = 1
    return output, True


def _destuff_with_consumed(
    raw_bits: list[int],
    *,
    max_output_bits: int,
) -> tuple[list[int], bool, int]:
    output: list[int] = []
    previous: int | None = None
    run = 0
    for index, bit in enumerate(raw_bits):
        if previous is not None and run == 5:
            if bit == previous:
                return output, False, index + 1
            previous = bit
            run = 1
            continue
        output.append(bit)
        if bit == previous:
            run += 1
        else:
            previous = bit
            run = 1
        if len(output) >= max_output_bits:
            return output, True, index + 1
    return output, True, len(raw_bits)


def _parse_header(raw_bits: list[int]) -> dict[str, Any] | None:
    prefix, stuffing_valid = _destuff(raw_bits, max_output_bits=14)
    if not stuffing_valid or len(prefix) < 14 or prefix[0] != 0:
        return None
    identifier_a = _integer(prefix[1:12])
    ide = prefix[13]
    if ide == 0:
        bits, stuffing_valid = _destuff(raw_bits, max_output_bits=17)
        if not stuffing_valid or len(bits) < 15:
            return None
        identifier = identifier_a
        fdf = bool(bits[14])
        return {
            "identifier": identifier,
            "extended": False,
            "fdf": fdf,
            "brs": bool(bits[16]) if fdf and len(bits) > 16 else False,
        }
    bits, stuffing_valid = _destuff(raw_bits, max_output_bits=37)
    if not stuffing_valid or len(bits) < 35 or bits[12] != 1:
        return None
    identifier = (identifier_a << 18) | _integer(bits[14:32])
    fdf = bool(bits[34])
    return {
        "identifier": identifier,
        "extended": True,
        "fdf": fdf,
        "brs": bool(bits[36]) if fdf and len(bits) > 36 else False,
    }


def _validate_classic_crc(
    raw_bits: list[int],
    header: dict[str, Any],
) -> dict[str, Any]:
    result = dict(header)
    if result["fdf"]:
        result["crc_valid"] = None
        return result
    if result["extended"]:
        prefix_length = 39
        data_start = 39
        dlc_slice = slice(35, 39)
        remote_index = 32
    else:
        prefix_length = 19
        data_start = 19
        dlc_slice = slice(15, 19)
        remote_index = 12
    prefix, valid = _destuff(raw_bits, max_output_bits=prefix_length)
    if not valid or len(prefix) < prefix_length:
        result["crc_valid"] = False
        return result
    dlc = min(_integer(prefix[dlc_slice]), 8)
    data_bits = 0 if prefix[remote_index] else dlc * 8
    required = data_start + data_bits + 15
    frame_bits, valid = _destuff(raw_bits, max_output_bits=required)
    if not valid or len(frame_bits) < required:
        result["crc_valid"] = False
        return result
    result["crc_valid"] = frame_bits[-15:] == can_crc15_bits(frame_bits[:-15])
    return result


def _parse_classic_frame(raw_bits: list[int]) -> dict[str, Any] | None:
    """Parse one sampled Classical CAN candidate without trusting invalid payloads."""
    header = _parse_header(raw_bits)
    if header is None or header["fdf"]:
        return None
    if header["extended"]:
        prefix_length = 39
        data_start = 39
        dlc_slice = slice(35, 39)
        remote_index = 32
    else:
        prefix_length = 19
        data_start = 19
        dlc_slice = slice(15, 19)
        remote_index = 12
    prefix, valid = _destuff(raw_bits, max_output_bits=prefix_length)
    if not valid or len(prefix) < prefix_length:
        return None
    remote = bool(prefix[remote_index])
    dlc = _integer(prefix[dlc_slice])
    payload_bit_count = 0 if remote else min(dlc, 8) * 8
    required = data_start + payload_bit_count + 15
    frame_bits, valid, consumed = _destuff_with_consumed(
        raw_bits, max_output_bits=required
    )
    if not valid or len(frame_bits) < required:
        return None
    if frame_bits[-15:] != can_crc15_bits(frame_bits[:-15]):
        return None
    trailer = raw_bits[consumed:consumed + 10]
    if (
        len(trailer) < 10
        or trailer[0] != 1
        or trailer[2] != 1
        or trailer[3:10] != [1] * 7
    ):
        return None
    payload = bytes(
        _integer(frame_bits[index:index + 8])
        for index in range(data_start, data_start + payload_bit_count, 8)
    )
    identifier = int(header["identifier"])
    return {
        "identifier": identifier,
        "identifier_hex": f"0x{identifier:08X}" if header["extended"] else f"0x{identifier:03X}",
        "extended": bool(header["extended"]),
        "remote": remote,
        "dlc": dlc,
        "payload_bytes": list(payload),
        "payload_hex": payload.hex().upper(),
        "crc_valid": True,
        "complete_raw_bit_count": consumed + 10,
    }


def _j1939_pgn(identifier: int) -> int:
    data_page = (identifier >> 24) & 0x1
    pdu_format = (identifier >> 16) & 0xFF
    pdu_specific = (identifier >> 8) & 0xFF
    return (data_page << 16) | (pdu_format << 8) | (
        pdu_specific if pdu_format >= 240 else 0
    )


def _infer_bitrates(edge_intervals_us: np.ndarray, sample_interval_us: float) -> list[int]:
    if edge_intervals_us.size < 4:
        return []
    matches: list[int] = []
    for bitrate in TIMING_BITRATES:
        period = 1_000_000.0 / bitrate
        samples_per_bit = period / sample_interval_us
        if samples_per_bit < 4.0:
            continue
        ratios = edge_intervals_us / period
        tolerance = max(0.14, sample_interval_us / period * 0.75)
        near_one = np.abs(ratios - 1.0) <= tolerance
        if np.count_nonzero(near_one) >= 2:
            matches.append(bitrate)
    return matches


def _frame_evidence(
    dominant: np.ndarray,
    *,
    samples_per_bit: float,
) -> tuple[list[np.ndarray], list[dict[str, Any]], float]:
    dominant_indices = np.flatnonzero(dominant)
    if not dominant_indices.size:
        return [], [], 0.0
    idle_samples = 10.5 * samples_per_bit
    split_points = np.flatnonzero(np.diff(dominant_indices) > idle_samples) + 1
    groups = [group for group in np.split(dominant_indices, split_points) if group.size]
    headers: list[dict[str, Any]] = []
    occupied_samples = 0.0
    for group in groups:
        start = int(group[0])
        last_dominant = int(group[-1])
        end = min(dominant.size, int(np.ceil(last_dominant + 11.0 * samples_per_bit)))
        occupied_samples += max(0, end - start)
        raw_bit_count = max(1, int(np.ceil((end - start) / samples_per_bit)))
        centers = start + (np.arange(raw_bit_count, dtype=np.float64) + 0.5) * samples_per_bit
        centers = centers[centers < dominant.size].astype(np.int64)
        raw_bits = [0 if dominant[index] else 1 for index in centers]
        header = _parse_header(raw_bits)
        if header is not None:
            headers.append(_validate_classic_crc(raw_bits, header))
    return groups, headers, occupied_samples


def _protocol_fingerprint(headers: list[dict[str, Any]], nominal_bitrate: int) -> dict[str, Any]:
    validated_headers = [header for header in headers if header.get("crc_valid") is True]
    if not validated_headers:
        return {
            "name": "Higher layer unresolved",
            "confidence": "low",
            "evidence": [
                "No CRC-valid Classical CAN frames were available for protocol fingerprinting"
            ],
            "known_pgns": [],
        }
    headers = validated_headers
    standard_identifiers = {
        int(header["identifier"])
        for header in headers
        if not header["extended"]
    }
    obd_requests = {
        identifier for identifier in standard_identifiers if 0x7E0 <= identifier <= 0x7E7
    }
    obd_responses = {
        identifier for identifier in standard_identifiers if 0x7E8 <= identifier <= 0x7EF
    }
    obd_functional = 0x7DF in standard_identifiers
    if obd_functional or (obd_requests and obd_responses):
        observed = sorted(
            standard_identifiers & ({0x7DF} | set(range(0x7E0, 0x7F0)))
        )
        return {
            "name": "ISO-TP / OBD-II diagnostic traffic",
            "confidence": "high" if obd_responses and (obd_functional or obd_requests) else "medium",
            "evidence": [
                *(f"0x{identifier:03X}" for identifier in observed),
                "11-bit diagnostic request/response range",
            ],
            "known_pgns": [],
        }

    canopen_heartbeats = sorted(
        identifier for identifier in standard_identifiers if 0x701 <= identifier <= 0x77F
    )
    canopen_sdo_requests = {
        identifier for identifier in standard_identifiers if 0x601 <= identifier <= 0x67F
    }
    canopen_sdo_responses = {
        identifier for identifier in standard_identifiers if 0x581 <= identifier <= 0x5FF
    }
    if canopen_heartbeats and canopen_sdo_requests and canopen_sdo_responses:
        return {
            "name": "CANopen",
            "confidence": "high",
            "evidence": [
                *(f"Heartbeat 0x{identifier:03X}" for identifier in canopen_heartbeats),
                "Matching SDO request/response COB-ID ranges",
            ],
            "known_pgns": [],
        }

    extended = [header for header in headers if header["extended"]]
    extended_ratio = len(extended) / len(headers) if headers else 0.0
    nmea_pgns = sorted(
        {
            pgn
            for header in extended
            if (pgn := _j1939_pgn(int(header["identifier"]))) in KNOWN_NMEA2000_PGNS
        }
    )
    if extended_ratio >= 0.8 and nominal_bitrate == 250_000 and nmea_pgns:
        return {
            "name": "NMEA 2000",
            "confidence": "high" if len(nmea_pgns) >= 2 else "medium",
            "evidence": [
                f"{extended_ratio:.0%} 29-bit identifiers",
                "250 kbit/s nominal rate",
                "Known NMEA 2000 PGNs observed",
            ],
            "known_pgns": nmea_pgns,
        }

    known_pgns = sorted(
        {
            pgn
            for header in extended
            if (pgn := _j1939_pgn(int(header["identifier"]))) in KNOWN_J1939_PGNS
        }
    )
    if extended_ratio >= 0.8 and nominal_bitrate in {250_000, 500_000} and known_pgns:
        return {
            "name": "SAE J1939",
            "confidence": "high" if len(known_pgns) >= 2 else "medium",
            "evidence": [
                f"{extended_ratio:.0%} 29-bit identifiers",
                f"{nominal_bitrate // 1000} kbit/s nominal rate",
                "Known PGNs observed",
            ],
            "known_pgns": known_pgns,
        }
    if extended_ratio >= 0.8:
        return {
            "name": "29-bit CAN; higher layer unresolved",
            "confidence": "low",
            "evidence": [f"{extended_ratio:.0%} 29-bit identifiers"],
            "known_pgns": [],
        }
    return {
        "name": "CAN; higher layer unresolved",
        "confidence": "low",
        "evidence": ["No strong higher-layer identifier fingerprint"],
        "known_pgns": [],
    }


def _decode_can_orientation(
    time_us: np.ndarray,
    can_h: np.ndarray,
    can_l: np.ndarray,
) -> dict[str, Any]:
    sample_interval_us = float(np.median(np.diff(time_us)))
    if sample_interval_us > 0.25:
        raise ValueError("CAN decode requires 0.25 us/sample or faster")
    differential = can_h - can_l
    low = float(np.quantile(differential, 0.01))
    high = float(np.quantile(differential, 0.99))
    if high - low < 0.5:
        return {"frames": [], "rejected_candidate_count": 0}
    dominant = differential > ((low + high) / 2.0)
    edges = np.flatnonzero(dominant[1:] != dominant[:-1]) + 1
    bitrate_matches = _infer_bitrates(np.diff(time_us[edges]), sample_interval_us)
    candidates: list[dict[str, Any]] = []
    for bitrate in (value for value in bitrate_matches if value in NOMINAL_BITRATES):
        samples_per_bit = (1_000_000.0 / bitrate) / sample_interval_us
        dominant_indices = np.flatnonzero(dominant)
        if not dominant_indices.size:
            continue
        split_points = np.flatnonzero(np.diff(dominant_indices) > 10.5 * samples_per_bit) + 1
        groups = [group for group in np.split(dominant_indices, split_points) if group.size]
        frames: list[dict[str, Any]] = []
        unsupported_fd_count = 0
        for group in groups:
            start = int(group[0])
            end = min(dominant.size, int(np.ceil(int(group[-1]) + 11.0 * samples_per_bit)))
            raw_bit_count = max(1, int(np.ceil((end - start) / samples_per_bit)))
            centers = start + (np.arange(raw_bit_count, dtype=np.float64) + 0.5) * samples_per_bit
            centers = centers[centers < dominant.size].astype(np.int64)
            raw_bits = [0 if dominant[index] else 1 for index in centers]
            frame = _parse_classic_frame(raw_bits)
            if frame is None:
                header = _parse_header(raw_bits)
                if header is not None and header.get("fdf"):
                    unsupported_fd_count += 1
                continue
            complete_end = min(
                dominant.size,
                int(np.ceil(start + int(frame["complete_raw_bit_count"]) * samples_per_bit)),
            )
            frame.pop("complete_raw_bit_count", None)
            frame.update({
                "timestamp_us": float(time_us[start] - time_us[0]),
                "nominal_bitrate_bps": bitrate,
                "source_sample_start": start,
                "source_sample_end": complete_end,
            })
            frames.append(frame)
        candidates.append({
            "nominal_bitrate_bps": bitrate,
            "frames": frames,
            "rejected_candidate_count": len(groups) - len(frames),
            "unsupported_fd_candidate_count": unsupported_fd_count,
        })
    if not candidates:
        return {"frames": [], "rejected_candidate_count": 0}
    return max(
        candidates,
        key=lambda item: (
            len(item["frames"]),
            -int(item["rejected_candidate_count"]),
            int(item["nominal_bitrate_bps"]),
        ),
    )


def aggregate_can_identifiers(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize validated frames deterministically without inventing one-frame cadence."""
    grouped: dict[tuple[int, bool], list[dict[str, Any]]] = {}
    for frame in frames:
        key = (int(frame["identifier"]), bool(frame["extended"]))
        grouped.setdefault(key, []).append(frame)

    summaries: list[dict[str, Any]] = []
    for (identifier, extended), observed in sorted(grouped.items()):
        ordered = sorted(observed, key=lambda item: float(item["timestamp_us"]))
        timestamps = [float(item["timestamp_us"]) for item in ordered]
        intervals = [later - earlier for earlier, later in zip(timestamps, timestamps[1:])]
        payloads = [list(item.get("payload_bytes", [])) for item in ordered]
        payload_states = [
            (
                bool(item.get("remote")),
                int(item.get("dlc", len(payload))),
                payload,
            )
            for item, payload in zip(ordered, payloads)
        ]
        payload_change_count = sum(
            current != previous
            for previous, current in zip(payload_states, payload_states[1:])
        )
        max_payload_length = max((len(payload) for payload in payloads), default=0)
        byte_change_counts = [0] * max_payload_length
        for previous, current in zip(payloads, payloads[1:]):
            for index in range(max_payload_length):
                previous_value = previous[index] if index < len(previous) else None
                current_value = current[index] if index < len(current) else None
                if previous_value != current_value:
                    byte_change_counts[index] += 1
        mean_period = sum(intervals) / len(intervals) if intervals else None
        summaries.append({
            "identifier": identifier,
            "identifier_hex": f"0x{identifier:08X}" if extended else f"0x{identifier:03X}",
            "extended": extended,
            "frame_count": len(ordered),
            "first_timestamp_us": timestamps[0],
            "last_timestamp_us": timestamps[-1],
            "observed_duration_us": timestamps[-1] - timestamps[0],
            "mean_period_us": mean_period,
            "mean_frequency_hz": 1_000_000.0 / mean_period if mean_period else None,
            "min_interval_us": min(intervals) if intervals else None,
            "max_interval_us": max(intervals) if intervals else None,
            "payload_change_count": payload_change_count,
            "last_payload_hex": str(ordered[-1].get("payload_hex", "")),
            "byte_change_counts": byte_change_counts,
        })
    return summaries


def decode_can_waveform(
    time_us: np.ndarray,
    can_h: np.ndarray,
    can_l: np.ndarray,
) -> dict[str, Any]:
    """Extract only complete CRC-valid Classical CAN frames from sampled H/L traces."""
    time_us = np.asarray(time_us, dtype=np.float64)
    can_h = np.asarray(can_h, dtype=np.float64)
    can_l = np.asarray(can_l, dtype=np.float64)
    if time_us.size < 3 or time_us.shape != can_h.shape or can_h.shape != can_l.shape:
        raise ValueError("CAN decode requires equal time, CAN-H, and CAN-L arrays")
    if not np.all(np.isfinite(time_us)) or not np.all(np.isfinite(can_h)) or not np.all(np.isfinite(can_l)):
        raise ValueError("CAN decode inputs must contain finite numeric samples")
    if np.any(np.diff(time_us) <= 0):
        raise ValueError("CAN decode timestamps must increase strictly")

    expected = _decode_can_orientation(time_us, can_h, can_l)
    if expected["frames"]:
        expected["polarity"] = "expected"
        expected["warnings"] = []
        return expected
    reversed_result = _decode_can_orientation(time_us, can_l, can_h)
    if reversed_result["frames"]:
        reversed_result["polarity"] = "reversed"
        reversed_result["warnings"] = [
            "CAN polarity is reversed relative to the recorded CAN-H/CAN-L source labels."
        ]
        return reversed_result
    expected["polarity"] = "expected"
    expected["warnings"] = []
    return expected


def _analyze_can_orientation(
    time_us: np.ndarray,
    can_h: np.ndarray,
    can_l: np.ndarray,
) -> dict[str, Any]:
    time_us = np.asarray(time_us, dtype=np.float64)
    can_h = np.asarray(can_h, dtype=np.float64)
    can_l = np.asarray(can_l, dtype=np.float64)
    if time_us.size < 3 or time_us.shape != can_h.shape or can_h.shape != can_l.shape:
        raise ValueError("CAN analysis requires equal time, CAN-H, and CAN-L arrays")

    sample_interval_us = float(np.median(np.diff(time_us)))
    if sample_interval_us > 0.25:
        return {
            "status": "insufficient_timing_evidence",
            "physical_layer": "CAN analysis resolution not met",
            "sample_interval_us": sample_interval_us,
            "warnings": [
                "Use the CAN Analyze window; passive classification requires 0.25 us/sample or faster."
            ],
        }
    differential = can_h - can_l
    recessive_level = float(np.quantile(differential, 0.01))
    dominant_level = float(np.quantile(differential, 0.99))
    differential_span = dominant_level - recessive_level
    correlation = float(np.corrcoef(can_h, can_l)[0, 1])
    if differential_span < 0.5:
        return {
            "status": "no_bus_activity",
            "physical_layer": "No CAN-family differential activity detected",
            "warnings": ["Differential amplitude is too small for passive CAN analysis."],
        }

    threshold = (recessive_level + dominant_level) / 2.0
    dominant = differential > threshold
    edges = np.flatnonzero(dominant[1:] != dominant[:-1]) + 1
    edge_intervals_us = np.diff(time_us[edges])
    bitrate_matches = _infer_bitrates(edge_intervals_us, sample_interval_us)
    if not bitrate_matches:
        return {
            "status": "insufficient_timing_evidence",
            "physical_layer": "High-speed CAN-family (ISO 11898-2)",
            "sample_interval_us": sample_interval_us,
            "warnings": ["Could not resolve a standard CAN nominal bitrate from the observed edges."],
        }

    candidates: list[tuple[int, float, list[np.ndarray], list[dict[str, Any]], float]] = []
    for bitrate in (value for value in bitrate_matches if value in NOMINAL_BITRATES):
        samples_per_bit = (1_000_000.0 / bitrate) / sample_interval_us
        groups, headers, occupied_samples = _frame_evidence(
            dominant,
            samples_per_bit=samples_per_bit,
        )
        if headers:
            candidates.append(
                (bitrate, samples_per_bit, groups, headers, occupied_samples)
            )
    if not candidates:
        return {
            "status": "insufficient_timing_evidence",
            "physical_layer": "High-speed CAN-family (ISO 11898-2)",
            "sample_interval_us": sample_interval_us,
            "warnings": ["Timing candidates did not produce valid CAN arbitration headers."],
        }
    nominal_bitrate, samples_per_bit, groups, headers, occupied_samples = max(
        candidates,
        key=lambda item: (
            len(item[3]),
            len(item[3]) / len(item[2]) if item[2] else 0.0,
            item[0],
        ),
    )

    extended_count = sum(bool(header["extended"]) for header in headers)
    standard_count = len(headers) - extended_count
    if extended_count and not standard_count:
        identifier_format = "29-bit"
    elif standard_count and not extended_count:
        identifier_format = "11-bit"
    else:
        identifier_format = "Mixed 11/29-bit"
    has_fd = any(bool(header["fdf"]) for header in headers)
    has_brs = any(bool(header["brs"]) for header in headers if header["fdf"])
    faster_rates = [bitrate for bitrate in bitrate_matches if bitrate > nominal_bitrate]
    bus_type = "CAN FD" if has_fd else "Classical CAN"
    protocol = _protocol_fingerprint(headers, nominal_bitrate)
    crc_valid_count = sum(header.get("crc_valid") is True for header in headers)
    confidence = (
        "high"
        if samples_per_bit >= 10.0 and crc_valid_count >= 3
        else "medium"
    )

    return {
        "status": "analyzed",
        "physical_layer": "High-speed CAN-family (ISO 11898-2)",
        "bus_type": bus_type,
        "nominal_bitrate_bps": nominal_bitrate,
        "data_bitrate_bps": max(faster_rates) if has_brs and faster_rates else None,
        "fd_brs_observed": has_brs,
        "samples_per_nominal_bit": round(samples_per_bit, 2),
        "observation_window_ms": round((time_us[-1] - time_us[0]) / 1000.0, 4),
        "bus_load_percent": round(occupied_samples / time_us.size * 100.0, 2),
        "bus_load_method": "Observed frame occupancy from SOF through 11 recessive bit times",
        "frame_count": len(groups),
        "decoded_header_count": len(headers),
        "crc_valid_header_count": crc_valid_count,
        "frame_rate_hz": round(len(groups) / ((time_us[-1] - time_us[0]) / 1_000_000.0), 2),
        "identifier_format": identifier_format,
        "protocol": protocol,
        "confidence": confidence,
        "signal_quality": {
            "can_h_can_l_correlation": correlation,
            "recessive_differential_v": recessive_level,
            "dominant_differential_v": dominant_level,
            "differential_span_v": differential_span,
        },
        "warnings": [],
    }


def analyze_can_waveform(
    time_us: np.ndarray,
    can_h: np.ndarray,
    can_l: np.ndarray,
) -> dict[str, Any]:
    """Retain the aggregate analysis contract while adding validated frame detail."""
    expected = _analyze_can_orientation(time_us, can_h, can_l)
    try:
        decoded = _decode_can_orientation(
            np.asarray(time_us, dtype=np.float64),
            np.asarray(can_h, dtype=np.float64),
            np.asarray(can_l, dtype=np.float64),
        )
    except ValueError:
        return expected

    result = expected
    frames = sorted(
        decoded["frames"],
        key=lambda frame: (
            int(frame["identifier"]),
            bool(frame["extended"]),
            float(frame["timestamp_us"]),
        ),
    )
    result.update({
        "can_polarity": "expected",
        "frames": frames,
        "identifiers": aggregate_can_identifiers(frames),
        "validated_frame_count": len(frames),
        "rejected_candidate_count": decoded["rejected_candidate_count"],
    })
    return result
