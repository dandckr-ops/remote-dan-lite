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
    for index, bit in enumerate(raw_bits):
        if previous is not None and run == 5:
            if bit == previous:
                return output, False
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
            if run == 5:
                if index + 1 >= len(raw_bits) or raw_bits[index + 1] == bit:
                    return output, False
            return output, True
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
            if run == 5:
                if index + 1 >= len(raw_bits) or raw_bits[index + 1] == bit:
                    return output, False, min(index + 2, len(raw_bits))
                return output, True, index + 2
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
    if header["extended"] and prefix[33] != 0:
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
        "ack_slot": "dominant" if trailer[1] == 0 else "recessive",
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


def _classify_positive_dominant_state(
    selected_differential: np.ndarray,
) -> tuple[np.ndarray, float, float] | None:
    """Separate two persistent differential states without assuming either occupancy."""
    values = np.asarray(selected_differential, dtype=np.float64)
    if values.size < 8:
        return None
    low = float(np.min(values))
    high = float(np.max(values))
    if high - low < 0.5:
        return None
    for _ in range(12):
        threshold = (low + high) / 2.0
        low_values = values[values <= threshold]
        high_values = values[values > threshold]
        if low_values.size < 4 or high_values.size < 4:
            return None
        next_low = float(np.median(low_values))
        next_high = float(np.median(high_values))
        if abs(next_low - low) < 1e-12 and abs(next_high - high) < 1e-12:
            low, high = next_low, next_high
            break
        low, high = next_low, next_high
    if high - low < 0.5 or high < 0.5:
        return None
    threshold = (low + high) / 2.0
    return values > threshold, low, high


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
        end = min(dominant.size, int(np.ceil(last_dominant + 20.0 * samples_per_bit)))
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
    states = _classify_positive_dominant_state(differential)
    if states is None:
        return {
            "frames": [],
            "rejected_candidate_count": 0,
            "unsupported_fd_candidate_count": 0,
            "differential_activity_detected": False,
        }
    dominant, _recessive_level, _dominant_level = states
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
            end = min(dominant.size, int(np.ceil(int(group[-1]) + 20.0 * samples_per_bit)))
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
        return {
            "frames": [],
            "rejected_candidate_count": 0,
            "unsupported_fd_candidate_count": 0,
            "differential_activity_detected": True,
        }
    selected = max(
        candidates,
        key=lambda item: (
            len(item["frames"]),
            int(item["unsupported_fd_candidate_count"]),
            -int(item["rejected_candidate_count"]),
            int(item["nominal_bitrate_bps"]),
        ),
    )
    selected["differential_activity_detected"] = True
    return selected


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
        payload_state_change_count = sum(
            current != previous
            for previous, current in zip(payload_states, payload_states[1:])
        )
        max_payload_length = max((len(payload) for payload in payloads), default=0)
        byte_change_counts = [0] * max_payload_length
        bit_change_counts = [[0] * 8 for _ in range(max_payload_length)]
        dlc_transition_count = 0
        rtr_data_transition_count = 0
        comparable_payload_transition_count = 0
        comparable_payload_change_count = 0
        introduced_byte_count = 0
        removed_byte_count = 0
        for previous_item, current_item, previous, current in zip(
            ordered, ordered[1:], payloads, payloads[1:]
        ):
            previous_remote = bool(previous_item.get("remote"))
            current_remote = bool(current_item.get("remote"))
            if int(previous_item.get("dlc", len(previous))) != int(
                current_item.get("dlc", len(current))
            ):
                dlc_transition_count += 1
            if previous_remote != current_remote:
                rtr_data_transition_count += 1
            if previous_remote or current_remote:
                continue
            comparable_payload_transition_count += 1
            shared_length = min(len(previous), len(current))
            comparable_changed = False
            for index in range(shared_length):
                previous_value = previous[index]
                current_value = current[index]
                if previous_value == current_value:
                    continue
                comparable_changed = True
                byte_change_counts[index] += 1
                changed = previous_value ^ current_value
                for bit_index in range(8):
                    if changed & (1 << (7 - bit_index)):
                        bit_change_counts[index][bit_index] += 1
            comparable_payload_change_count += int(comparable_changed)
            introduced_byte_count += max(0, len(current) - len(previous))
            removed_byte_count += max(0, len(previous) - len(current))
        mean_period = sum(intervals) / len(intervals) if intervals else None
        inter_arrival_stddev = (
            float(np.std(np.asarray(intervals, dtype=np.float64)))
            if len(intervals) >= 3 else None
        )
        transition_count = len(payload_states) - 1
        summaries.append({
            "identifier": identifier,
            "identifier_hex": f"0x{identifier:08X}" if extended else f"0x{identifier:03X}",
            "extended": extended,
            "frame_count": len(ordered),
            "first_timestamp_us": timestamps[0],
            "last_timestamp_us": timestamps[-1],
            "observed_duration_us": timestamps[-1] - timestamps[0],
            "interval_count": len(intervals),
            "mean_period_us": mean_period,
            "mean_frequency_hz": 1_000_000.0 / mean_period if mean_period else None,
            "min_interval_us": min(intervals) if intervals else None,
            "median_interval_us": float(np.median(intervals)) if intervals else None,
            "max_interval_us": max(intervals) if intervals else None,
            "inter_arrival_stddev_us": inter_arrival_stddev,
            "inter_arrival_stddev_measure": "population standard deviation; reported only with at least 3 intervals",
            "payload_state_transition_count": transition_count,
            "payload_state_change_count": payload_state_change_count,
            "payload_state_change_percent": (
                payload_state_change_count / transition_count * 100.0
                if transition_count else None
            ),
            "dlc_transition_count": dlc_transition_count,
            "rtr_data_transition_count": rtr_data_transition_count,
            "comparable_payload_transition_count": comparable_payload_transition_count,
            "comparable_payload_change_count": comparable_payload_change_count,
            "introduced_byte_count": introduced_byte_count,
            "removed_byte_count": removed_byte_count,
            "last_payload_hex": str(ordered[-1].get("payload_hex", "")),
            "byte_change_counts": byte_change_counts,
            "bit_change_counts": bit_change_counts,
        })
    return summaries


def _level_summary(values: np.ndarray) -> dict[str, float]:
    return {
        "median": float(np.median(values)),
        "p05": float(np.quantile(values, 0.05)),
        "p95": float(np.quantile(values, 0.95)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def _state_level_summary(
    can_h: np.ndarray,
    can_l: np.ndarray,
    selected_differential: np.ndarray,
    mask: np.ndarray,
) -> dict[str, Any]:
    common_mode = (can_h + can_l) / 2.0
    return {
        "sample_count": int(np.count_nonzero(mask)),
        "can_h_v": _level_summary(can_h[mask]),
        "can_l_v": _level_summary(can_l[mask]),
        "differential_v": _level_summary(selected_differential[mask]),
        "common_mode_v": _level_summary(common_mode[mask]),
    }


def _transition_estimates(
    time_us: np.ndarray,
    selected_differential: np.ndarray,
    recessive_level: float,
    dominant_level: float,
) -> dict[str, Any]:
    threshold = (recessive_level + dominant_level) / 2.0
    states = selected_differential > threshold
    return {
        "available": False,
        "rise_time_us": None,
        "fall_time_us": None,
        "edge_count": int(np.count_nonzero(states[1:] != states[:-1])),
        "sample_interval_us": float(np.median(np.diff(time_us))),
        "dispersion_us": None,
        "method": "unavailable in analyzer v2",
        "reason": "validated-transition 10%-to-90% interpolation is not implemented in this release",
    }


def build_can_diagnostics(
    time_us: np.ndarray,
    can_h: np.ndarray,
    can_l: np.ndarray,
    decoded: dict[str, Any],
) -> dict[str, Any]:
    """Build bounded v2 physical and traffic diagnostics from selected passive evidence."""
    time_us = np.asarray(time_us, dtype=np.float64)
    can_h = np.asarray(can_h, dtype=np.float64)
    can_l = np.asarray(can_l, dtype=np.float64)
    if time_us.size < 3 or time_us.shape != can_h.shape or can_h.shape != can_l.shape:
        raise ValueError("CAN diagnostics require equal sampled H/L arrays")
    polarity = str(decoded.get("polarity"))
    if polarity not in {"expected", "reversed"}:
        raise ValueError("CAN diagnostics require selected polarity")
    selected_differential = can_h - can_l if polarity == "expected" else can_l - can_h
    states = _classify_positive_dominant_state(selected_differential)
    if states is None:
        raise ValueError("CAN diagnostics require state-separated differential activity")
    dominant_mask, _recessive_level, _dominant_level = states
    recessive_mask = ~dominant_mask
    if not np.any(dominant_mask) or not np.any(recessive_mask):
        raise ValueError("CAN diagnostics require dominant and recessive samples")
    dominant = _state_level_summary(
        can_h, can_l, selected_differential, dominant_mask
    )
    recessive = _state_level_summary(
        can_h, can_l, selected_differential, recessive_mask
    )
    sample_interval_us = float(np.median(np.diff(time_us)))
    capture_duration_us = float(time_us[-1] - time_us[0])
    physical = {
        "capture_duration_us": capture_duration_us,
        "sample_interval_us": sample_interval_us,
        "selected_polarity": polarity,
        "state_method": "median recessive level and positive samples separated by at least 0.5 V",
        "dominant": dominant,
        "recessive": recessive,
        "differential_span_v": (
            float(dominant["differential_v"]["median"])
            - float(recessive["differential_v"]["median"])
        ),
        "transition_timing": _transition_estimates(
            time_us,
            selected_differential,
            float(recessive["differential_v"]["median"]),
            float(dominant["differential_v"]["median"]),
        ),
        "interpretation": "sampled voltage levels only; no resistance or bus-health conclusion",
    }

    frames = list(decoded.get("frames", []))
    occupied_intervals = sorted(
        (int(frame["source_sample_start"]), int(frame["source_sample_end"]))
        for frame in frames
        if int(frame["source_sample_start"]) > 0
        and int(frame["source_sample_end"]) < time_us.size
    )
    occupied_samples = 0
    union_start: int | None = None
    union_end: int | None = None
    for start, end in occupied_intervals:
        if not 0 <= start < end <= time_us.size:
            raise ValueError("CAN frame occupancy falls outside source samples")
        if union_start is None:
            union_start, union_end = start, end
        elif start > int(union_end):
            occupied_samples += int(union_end) - int(union_start)
            union_start, union_end = start, end
        else:
            union_end = max(int(union_end), end)
    if union_start is not None:
        occupied_samples += int(union_end) - int(union_start)
    ack_counts = Counter(str(frame.get("ack_slot")) for frame in frames)
    dlc_counts = Counter(int(frame["dlc"]) for frame in frames)
    duration_s = capture_duration_us / 1_000_000.0
    validated_count = len(frames)
    capture_sample_interval_count = int(time_us.size - 1)
    integrity = {
        "capture_duration_us": capture_duration_us,
        "observed_frame_count": validated_count,
        "observed_window_frame_rate_hz": (
            validated_count / duration_s if duration_s > 0 else None
        ),
        "observed_window_rate_numerator_frame_count": validated_count,
        "observed_window_rate_denominator_seconds": duration_s,
        "rate_qualification": "projected from bounded observed window; capture boundaries are censored",
        "capture_boundary_censoring": {
            "leading_interval_censored": True,
            "trailing_interval_censored": True,
        },
        "validated_classical_frame_wire_occupancy_percent": (
            occupied_samples / capture_sample_interval_count * 100.0
        ),
        "validated_classical_frame_wire_occupied_sample_interval_count": occupied_samples,
        "capture_sample_interval_count": capture_sample_interval_count,
        "validated_classical_frame_wire_occupancy_method": "union of complete validated Classical CAN frame source intervals divided by capture sample intervals",
        "validated_classical_frame_wire_occupancy_excludes": [
            "rejected or undecodable traffic",
            "CAN FD candidates",
            "frames clipped by either capture boundary",
            "intermission outside each complete validated frame interval",
        ],
        "validated_frame_count": validated_count,
        "rejected_candidate_count": int(decoded.get("rejected_candidate_count", 0)),
        "unsupported_fd_candidate_count": int(
            decoded.get("unsupported_fd_candidate_count", 0)
        ),
        "ack_dominant_count": ack_counts["dominant"],
        "ack_recessive_count": ack_counts["recessive"],
        "ack_dominant_rate_percent": (
            ack_counts["dominant"] / validated_count * 100.0
            if validated_count else None
        ),
        "standard_frame_count": sum(not bool(frame["extended"]) for frame in frames),
        "extended_frame_count": sum(bool(frame["extended"]) for frame in frames),
        "remote_frame_count": sum(bool(frame["remote"]) for frame in frames),
        "dlc_distribution": {
            str(dlc): count for dlc, count in sorted(dlc_counts.items())
        },

        "provenance": {
            "frame_authority": "CRC-valid Classical CAN frames",
            "occupancy_authority": "validated source sample intervals",
            "selected_polarity": polarity,
        },
        "capabilities": {
            "sampled_waveform_analysis_available": True,
            "scope_acquisition_available": True,
            "long_listen_only_can_adapter_available": False,
            "socketcan_or_provider_available": False,
            "transmit_available": False,
            "replay_available": False,
            "query_available": False,
        },
    }
    return {
        "physical_layer_diagnostics": physical,
        "integrity_diagnostics": integrity,
    }


def build_can_timeline(
    frames: list[dict[str, Any]],
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return a deterministic bounded projection of already validated frames."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("timeline limit must be between 1 and 200")
    ordered = sorted(
        frames,
        key=lambda frame: (
            float(frame["timestamp_us"]),
            int(frame["identifier"]),
            bool(frame["extended"]),
        ),
    )
    fields = (
        "timestamp_us", "identifier", "identifier_hex", "extended", "remote",
        "dlc", "payload_hex", "ack_slot",
    )
    return [{name: frame[name] for name in fields} for frame in ordered[:limit]]


def build_can_payload_heatmap(
    identifiers: list[dict[str, Any]],
    *,
    capture_duration_us: float,
    identifier_filter: str = "",
    changing_only: bool = False,
    identifier_limit: int = 200,
) -> dict[str, Any]:
    """Build a fixed 64-bit payload-change matrix from bounded identifier summaries."""
    if not 1 <= identifier_limit <= 200:
        raise ValueError("heatmap identifier limit must be between 1 and 200")
    if not np.isfinite(capture_duration_us) or capture_duration_us <= 0:
        raise ValueError("heatmap capture duration must be positive")
    normalized_filter = identifier_filter.strip().lower().removeprefix("0x")
    if len(normalized_filter) > 16 or any(
        character not in "0123456789abcdef" for character in normalized_filter
    ):
        raise ValueError("invalid heatmap identifier filter")

    def matches(item: dict[str, Any]) -> bool:
        normalized = str(item.get("identifier_hex", "")).lower().removeprefix("0x")
        return (
            (not normalized_filter or normalized_filter in normalized)
            and (
                not changing_only
                or int(item.get("payload_state_change_count", 0)) > 0
            )
        )

    filtered = [item for item in identifiers if isinstance(item, dict) and matches(item)]
    ranked = sorted(
        filtered,
        key=lambda item: (
            -int(item.get("payload_state_change_count", 0)),
            -int(item.get("frame_count", 0)),
            int(item["identifier"]),
            bool(item["extended"]),
        ),
    )
    selected = ranked[:identifier_limit]
    rows: list[dict[str, Any]] = []
    for item in selected:
        counts = [
            int(value)
            for byte_counts in item.get("bit_change_counts", [])[:8]
            for value in list(byte_counts)[:8]
        ]
        counts.extend([0] * (64 - len(counts)))
        rows.append({
            "key": {
                "identifier": int(item["identifier"]),
                "extended": bool(item["extended"]),
            },
            "identifier_hex": str(item["identifier_hex"]),
            "format": "extended" if item["extended"] else "standard",
            "frame_count": int(item.get("frame_count", 0)),
            "payload_state_change_count": int(
                item.get("payload_state_change_count", 0)
            ),
            "introduced_byte_count": int(item.get("introduced_byte_count", 0)),
            "removed_byte_count": int(item.get("removed_byte_count", 0)),
            "cells": counts,
        })
    bin_count = 64
    return {
        "source_identifier_count": len(identifiers),
        "total_identifier_count": len(filtered),
        "returned_identifier_count": len(rows),
        "identifiers_truncated": len(filtered) > len(rows),
        "identifier_limit": identifier_limit,
        "bin_count": bin_count,
        "bin_width_bits": 1,
        "cell_limit": identifier_limit * bin_count,
        "returned_cell_count": len(rows) * bin_count,
        "capture_interval_us": {"start": 0.0, "end": float(capture_duration_us)},
        "ranking_policy": "payload-state changes descending, frame count descending, identifier ascending, standard before extended",
        "selection_policy": "identifier filtering and changing-only selection are applied before deterministic ranking and limiting",
        "cell_semantics": "count of comparable payload bit flips where both adjacent frames are data and the byte exists in both; DLC growth, shrink, and RTR/data transitions are excluded",
        "identifiers": rows,
    }


def _numeric_delta(baseline: object, candidate: object) -> float | int | None:
    if (
        isinstance(baseline, (int, float))
        and not isinstance(baseline, bool)
        and isinstance(candidate, (int, float))
        and not isinstance(candidate, bool)
        and np.isfinite(float(baseline))
        and np.isfinite(float(candidate))
    ):
        return candidate - baseline
    return None


def compare_can_decode_results(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    identifier_limit: int = 200,
) -> dict[str, Any]:
    """Compare two authoritative v2 projections with explicit compatibility truth."""
    if baseline.get("artifact_schema_version") != 2 or candidate.get(
        "artifact_schema_version"
    ) != 2:
        raise ValueError("CAN comparison requires compatible artifact schema version 2")
    if baseline.get("run_id") == candidate.get("run_id"):
        raise ValueError("CAN comparison requires different runs")
    if isinstance(identifier_limit, bool) or not isinstance(identifier_limit, int):
        raise ValueError("comparison identifier limit is invalid")
    if not 1 <= identifier_limit <= 200:
        raise ValueError("comparison identifier limit is invalid")

    compatibility_fields = (
        ("decoder algorithm", "decoder_algorithm_version"),
        ("analyzer", "analyzer_version"),
        ("decoder settings", "decoder_settings"),
        ("nominal bitrate", "nominal_bitrate_bps"),
        ("polarity", "can_polarity"),
    )
    compatibility: dict[str, Any] = {
        "artifact_schema": {
            "baseline": baseline.get("artifact_schema_version"),
            "candidate": candidate.get("artifact_schema_version"),
            "compatible": True,
        }
    }
    for label, field in compatibility_fields:
        equal = baseline.get(field) == candidate.get(field)
        compatibility[field] = {
            "baseline": baseline.get(field),
            "candidate": candidate.get(field),
            "compatible": equal,
        }
        if not equal:
            raise ValueError(f"CAN comparison {label} is incompatible")

    baseline_physical = baseline.get("physical_layer_diagnostics", {})
    candidate_physical = candidate.get("physical_layer_diagnostics", {})
    baseline_interval = baseline_physical.get("sample_interval_us")
    candidate_interval = candidate_physical.get("sample_interval_us")
    interval_equal = _numeric_delta(baseline_interval, candidate_interval) == 0
    compatibility["sample_interval"] = {
        "baseline_us": baseline_interval,
        "candidate_us": candidate_interval,
        "compatible": interval_equal,
    }
    if not interval_equal:
        raise ValueError("CAN comparison sample interval is incompatible")
    baseline_duration = baseline_physical.get("capture_duration_us")
    candidate_duration = candidate_physical.get("capture_duration_us")
    if (
        _numeric_delta(baseline_duration, candidate_duration) is None
        or float(baseline_duration) <= 0
        or float(candidate_duration) <= 0
    ):
        raise ValueError("CAN comparison capture duration is malformed")
    durations_equal = float(baseline_duration) == float(candidate_duration)
    compatibility["duration"] = {
        "baseline_us": baseline_duration,
        "candidate_us": candidate_duration,
        "equal": durations_equal,
        "qualification": (
            "equal bounded observation windows; observed-window rates remain capture-boundary censored"
            if durations_equal
            else "unequal bounded observation windows; compare observed-window rates only, not raw counts as traffic conclusions"
        ),
    }

    baseline_items = baseline.get("identifiers")
    candidate_items = candidate.get("identifiers")
    if not isinstance(baseline_items, list) or not isinstance(candidate_items, list):
        raise ValueError("CAN comparison identifiers are malformed")

    def index(items: list[dict[str, Any]]) -> dict[tuple[int, bool], dict[str, Any]]:
        result: dict[tuple[int, bool], dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("CAN comparison identifier is malformed")
            key = (int(item["identifier"]), bool(item["extended"]))
            if key in result:
                raise ValueError("CAN comparison identifier is duplicated")
            result[key] = item
        return result

    baseline_by_key = index(baseline_items)
    candidate_by_key = index(candidate_items)
    baseline_keys = set(baseline_by_key)
    candidate_keys = set(candidate_by_key)

    def key_projection(key: tuple[int, bool]) -> dict[str, Any]:
        identifier, extended = key
        return {
            "identifier": identifier,
            "identifier_hex": (
                f"0x{identifier:08X}" if extended else f"0x{identifier:03X}"
            ),
            "extended": extended,
        }

    common_keys = sorted(baseline_keys & candidate_keys)
    only_baseline_keys = sorted(baseline_keys - candidate_keys)
    only_candidate_keys = sorted(candidate_keys - baseline_keys)
    baseline_duration_s = float(baseline_duration) / 1_000_000.0
    candidate_duration_s = float(candidate_duration) / 1_000_000.0
    deltas: list[dict[str, Any]] = []
    for key in common_keys[:identifier_limit]:
        before = baseline_by_key[key]
        after = candidate_by_key[key]
        baseline_rate = int(before["frame_count"]) / baseline_duration_s
        candidate_rate = int(after["frame_count"]) / candidate_duration_s
        deltas.append({
            **key_projection(key),
            "baseline_observed_frame_count": int(before["frame_count"]),
            "candidate_observed_frame_count": int(after["frame_count"]),
            "count_qualification": compatibility["duration"]["qualification"],
            "baseline_observed_window_rate_hz": baseline_rate,
            "candidate_observed_window_rate_hz": candidate_rate,
            "observed_window_rate_hz_delta": candidate_rate - baseline_rate,
            "baseline_mean_period_us": before.get("mean_period_us"),
            "candidate_mean_period_us": after.get("mean_period_us"),
            "mean_period_us_delta": _numeric_delta(
                before.get("mean_period_us"), after.get("mean_period_us")
            ),
            "baseline_payload_state_change_percent": before.get(
                "payload_state_change_percent"
            ),
            "candidate_payload_state_change_percent": after.get(
                "payload_state_change_percent"
            ),
            "payload_state_change_percent_delta": _numeric_delta(
                before.get("payload_state_change_percent"),
                after.get("payload_state_change_percent"),
            ),
        })

    physical_deltas: dict[str, float | int] = {}
    for name in ("capture_duration_us", "sample_interval_us", "differential_span_v"):
        delta = _numeric_delta(baseline_physical.get(name), candidate_physical.get(name))
        if delta is not None:
            physical_deltas[name] = delta
    for state in ("dominant", "recessive"):
        for signal, suffix in (
            ("can_h_v", "can_h"), ("can_l_v", "can_l"),
            ("differential_v", "differential"), ("common_mode_v", "common_mode"),
        ):
            before_levels = baseline_physical.get(state, {}).get(signal, {})
            after_levels = candidate_physical.get(state, {}).get(signal, {})
            for measure in ("median", "p05", "p95"):
                delta = _numeric_delta(
                    before_levels.get(measure), after_levels.get(measure)
                )
                if delta is not None:
                    physical_deltas[f"{state}_{suffix}_{measure}_v"] = delta

    baseline_integrity = baseline.get("integrity_diagnostics", {})
    candidate_integrity = candidate.get("integrity_diagnostics", {})
    integrity_deltas: dict[str, float | int] = {}
    for name in (
        "observed_window_frame_rate_hz",
        "validated_classical_frame_wire_occupancy_percent",
        "ack_dominant_rate_percent",
    ):
        delta = _numeric_delta(baseline_integrity.get(name), candidate_integrity.get(name))
        if delta is not None:
            integrity_deltas[name] = delta

    provenance_fields = (
        "run_id", "capture_id", "captured_at", "source_run_id", "source_capture_id",
        "source_captured_at", "source_sha256", "source_manifest_sha256",
    )
    provenance = {
        "baseline": {name: baseline.get(name) for name in provenance_fields},
        "candidate": {name: candidate.get(name) for name in provenance_fields},
        "authority": "complete authoritative can_decode child chains",
    }
    same_source = baseline.get("source_sha256") == candidate.get("source_sha256")
    common_returned = common_keys[:identifier_limit]
    baseline_only_returned = only_baseline_keys[:identifier_limit]
    candidate_only_returned = only_candidate_keys[:identifier_limit]
    return {
        "artifact_schema_version": 2,
        "decoder_algorithm_version": baseline.get("decoder_algorithm_version"),
        "analyzer_version": baseline.get("analyzer_version"),
        "provenance": provenance,
        "same_source_warning": (
            "Baseline and candidate use the same source waveform hash; differences should be expected to be zero."
            if same_source else None
        ),
        "compatibility": compatibility,
        "limits": {"identifier_delta_limit": identifier_limit},
        "identifier_delta_total_count": len(common_keys),
        "identifier_delta_returned_count": len(deltas),
        "identifier_deltas_truncated": len(common_keys) > len(deltas),
        "observed_only_in_baseline_total_count": len(only_baseline_keys),
        "observed_only_in_baseline_returned_count": len(baseline_only_returned),
        "observed_only_in_baseline_truncated": len(only_baseline_keys) > len(baseline_only_returned),
        "observed_only_in_candidate_total_count": len(only_candidate_keys),
        "observed_only_in_candidate_returned_count": len(candidate_only_returned),
        "observed_only_in_candidate_truncated": len(only_candidate_keys) > len(candidate_only_returned),
        "common_identifier_total_count": len(common_keys),
        "common_identifier_returned_count": len(common_returned),
        "common_identifiers_truncated": len(common_keys) > len(common_returned),
        "observed_only_in_baseline": [key_projection(key) for key in baseline_only_returned],
        "observed_only_in_candidate": [key_projection(key) for key in candidate_only_returned],
        "common_identifiers": [key_projection(key) for key in common_returned],
        "identifier_deltas": deltas,
        "physical_deltas": physical_deltas,
        "integrity_deltas": integrity_deltas,
    }


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
    reversed_result = _decode_can_orientation(time_us, can_l, can_h)
    orientations = (("expected", expected), ("reversed", reversed_result))

    def evidence_key(
        item: tuple[str, dict[str, Any]],
    ) -> tuple[int, int, int, float, int, int]:
        polarity, result = item
        frame_count = len(result["frames"])
        unsupported_fd_count = int(result.get("unsupported_fd_candidate_count", 0))
        rejected_count = int(result["rejected_candidate_count"])
        candidate_count = frame_count + unsupported_fd_count + rejected_count
        rejected_ratio = rejected_count / candidate_count if candidate_count else 0.0
        return (
            frame_count,
            int(bool(result.get("differential_activity_detected"))),
            unsupported_fd_count,
            -rejected_ratio,
            -rejected_count,
            int(polarity == "expected"),
        )

    polarity, selected = max(orientations, key=evidence_key)
    selected["polarity"] = polarity
    selected["warnings"] = (
        [
            "CAN polarity is reversed relative to the recorded CAN-H/CAN-L source labels."
        ]
        if polarity == "reversed"
        else []
    )
    return selected


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
    states = _classify_positive_dominant_state(differential)
    correlation = float(np.corrcoef(can_h, can_l)[0, 1])
    if states is None:
        return {
            "status": "no_bus_activity",
            "physical_layer": "No CAN-family differential activity detected",
            "warnings": ["Differential amplitude is too small for passive CAN analysis."],
        }

    dominant, recessive_level, dominant_level = states
    differential_span = dominant_level - recessive_level
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
    try:
        decoded = decode_can_waveform(
            np.asarray(time_us, dtype=np.float64),
            np.asarray(can_h, dtype=np.float64),
            np.asarray(can_l, dtype=np.float64),
        )
    except ValueError:
        return _analyze_can_orientation(time_us, can_h, can_l)

    polarity = str(decoded["polarity"])
    result = _analyze_can_orientation(
        time_us,
        can_h if polarity == "expected" else can_l,
        can_l if polarity == "expected" else can_h,
    )
    frames = sorted(
        decoded["frames"],
        key=lambda frame: (
            int(frame["identifier"]),
            bool(frame["extended"]),
            float(frame["timestamp_us"]),
        ),
    )
    warnings = list(result.get("warnings", []))
    for warning in decoded.get("warnings", []):
        if warning not in warnings:
            warnings.append(warning)
    result.update({
        "can_polarity": polarity,
        "warnings": warnings,
        "frames": frames,
        "identifiers": aggregate_can_identifiers(frames),
        "validated_frame_count": len(frames),
        "rejected_candidate_count": decoded["rejected_candidate_count"],
    })
    return result
