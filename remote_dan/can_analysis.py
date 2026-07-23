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


def analyze_can_waveform(
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
