from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from remote_dan.can_analysis import analyze_can_waveform


VERIFIED_HARNESSES = {
    "can-network",
    "protected-differential",
    "protected-single-ended",
}
SERIAL_RATES = (
    300,
    600,
    1200,
    2400,
    4800,
    9600,
    10_400,
    19_200,
    38_400,
    57_600,
    115_200,
    230_400,
    460_800,
    921_600,
    1_000_000,
)


@dataclass(frozen=True)
class SurveySegment:
    name: str
    time_us: np.ndarray
    channels: dict[str, np.ndarray]
    overflow_channels: tuple[str, ...] = ()

    @property
    def sample_interval_us(self) -> float:
        if self.time_us.size < 2:
            raise ValueError("survey segment needs at least two samples")
        interval = float(np.median(np.diff(self.time_us)))
        if not np.isfinite(interval) or interval <= 0:
            raise ValueError("survey segment timebase must increase")
        return interval


def _percentiles(values: np.ndarray) -> dict[str, float]:
    q01, q05, q10, q50, q90, q95, q99 = np.percentile(
        values, [1, 5, 10, 50, 90, 95, 99]
    )
    return {
        "min": float(np.min(values)),
        "q01": float(q01),
        "q05": float(q05),
        "q10": float(q10),
        "median": float(q50),
        "q90": float(q90),
        "q95": float(q95),
        "q99": float(q99),
        "max": float(np.max(values)),
        "span_98": float(q99 - q01),
        "mean": float(np.mean(values)),
    }


def _validate_segment(segment: SurveySegment) -> None:
    count = segment.time_us.size
    if count < 2 or not np.all(np.isfinite(segment.time_us)):
        raise ValueError("survey timebase must contain finite samples")
    _ = segment.sample_interval_us
    if not segment.channels:
        raise ValueError("survey segment needs at least one channel")
    for name, values in segment.channels.items():
        if values.size != count:
            raise ValueError(f"survey channel {name} length does not match timebase")
        if not np.all(np.isfinite(values)):
            raise ValueError(f"survey channel {name} contains non-finite samples")


def _rail_fraction(values: np.ndarray) -> float:
    q10, q90 = np.percentile(values, [10, 90])
    span = float(q90 - q10)
    if span <= 0:
        return 0.0
    tolerance = max(0.05, span * 0.12)
    on_rails = (values <= q10 + tolerance) | (values >= q90 - tolerance)
    return float(np.mean(on_rails))


def _infer_edge_rates(
    values: np.ndarray,
    *,
    sample_interval_us: float,
) -> list[dict[str, float | int]]:
    q10, q90 = np.percentile(values, [10, 90])
    if q90 - q10 < 0.12:
        return []
    state = values > ((q10 + q90) / 2.0)
    edge_indices = np.flatnonzero(np.diff(state.astype(np.int8)) != 0) + 1
    if edge_indices.size < 6:
        return []
    intervals = np.diff(edge_indices).astype(np.float64) * sample_interval_us
    results: list[dict[str, float | int]] = []
    for rate in SERIAL_RATES:
        period_us = 1_000_000.0 / rate
        if period_us / sample_interval_us < 4.0:
            continue
        ratios = intervals / period_us
        if float(np.min(ratios)) < 0.72:
            continue
        nearest = np.rint(ratios)
        valid = nearest >= 1
        if not np.any(valid):
            continue
        error = np.abs(ratios[valid] - nearest[valid])
        fit = float(np.mean(error <= 0.16))
        near_one = int(np.count_nonzero((ratios >= 0.84) & (ratios <= 1.16)))
        if fit < 0.72 or near_one < 2:
            continue
        results.append({
            "rate_bps": rate,
            "fit": fit,
            "one_bit_intervals": near_one,
            "samples_per_bit": period_us / sample_interval_us,
        })
    results.sort(
        key=lambda item: (
            int(item["one_bit_intervals"]),
            float(item["fit"]),
            -int(item["rate_bps"]),
        ),
        reverse=True,
    )
    return results[:4]


def _validate_uart(
    values: np.ndarray,
    *,
    sample_interval_us: float,
    rate_bps: int,
) -> dict[str, object]:
    samples_per_bit = (1_000_000.0 / rate_bps) / sample_interval_us
    if samples_per_bit < 8.0:
        return {"valid": False, "reason": "fewer than 8 samples per candidate bit"}
    q10, q90 = np.percentile(values, [10, 90])
    physical = values > ((q10 + q90) / 2.0)
    best: dict[str, object] = {"valid_frames": 0, "invalid_starts": 0}
    for inverted in (False, True):
        logical = np.logical_not(physical) if inverted else physical
        starts = np.flatnonzero(logical[:-1] & ~logical[1:]) + 1
        decoded: list[int] = []
        invalid = 0
        cursor = 0
        for start in starts:
            if start < cursor:
                continue
            data_indices = np.rint(
                start + (1.5 + np.arange(8, dtype=np.float64)) * samples_per_bit
            ).astype(np.int64)
            start_index = int(round(start + 0.5 * samples_per_bit))
            stop_index = int(round(start + 9.5 * samples_per_bit))
            if stop_index >= logical.size:
                continue
            if logical[start_index] or not logical[stop_index]:
                invalid += 1
                continue
            value = sum(int(logical[index]) << bit for bit, index in enumerate(data_indices))
            decoded.append(value)
            cursor = int(round(start + 9.8 * samples_per_bit))
        candidate = {
            "valid_frames": len(decoded),
            "invalid_starts": invalid,
            "valid_fraction": len(decoded) / max(len(decoded) + invalid, 1),
            "distinct_values": len(set(decoded)),
            "inverted": inverted,
            "preview_hex": bytes(decoded[:24]).hex(),
        }
        if int(candidate["valid_frames"]) > int(best["valid_frames"]):
            best = candidate
    best["valid"] = (
        int(best["valid_frames"]) >= 20
        and float(best["valid_fraction"]) >= 0.95
        and int(best["distinct_values"]) > 1
    )
    return best


def _uart_windows(
    segments: Sequence[SurveySegment],
    *,
    rate_bps: int | None,
    signal_for_segment,
) -> dict[str, object]:
    if rate_bps is None:
        return {"valid_windows": 0, "windows": []}
    windows: list[dict[str, object]] = []
    for item in segments:
        values = signal_for_segment(item)
        if values is None:
            continue
        validation = _validate_uart(
            values,
            sample_interval_us=item.sample_interval_us,
            rate_bps=rate_bps,
        )
        windows.append({"segment": item.name, **validation})
    return {
        "valid_windows": sum(bool(item.get("valid")) for item in windows),
        "windows": windows,
    }


def _best_uart_candidate(segments: Sequence[SurveySegment], *, signal_for_segment):
    rates: set[int] = set()
    per_window_rates: list[dict[str, object]] = []
    for item in segments:
        values = signal_for_segment(item)
        if values is None:
            continue
        candidates = _infer_edge_rates(
            values, sample_interval_us=item.sample_interval_us
        )
        per_window_rates.append({"segment": item.name, "candidates": candidates})
        rates.update(int(candidate["rate_bps"]) for candidate in candidates)
    ranked: list[tuple[tuple[int, int], int, dict[str, object]]] = []
    for rate in rates:
        evidence = _uart_windows(
            segments, rate_bps=rate, signal_for_segment=signal_for_segment
        )
        valid_frames = sum(
            int(window.get("valid_frames", 0)) for window in evidence["windows"]
        )
        ranked.append(((int(evidence["valid_windows"]), valid_frames), rate, evidence))
    ranked.sort(reverse=True)
    if not ranked:
        return None, {"valid_windows": 0, "windows": [], "candidate_rates_by_window": per_window_rates}
    _, rate, evidence = ranked[0]
    evidence["candidate_rates_by_window"] = per_window_rates
    return rate, evidence


def _base_result(
    *,
    status: str,
    topology: str,
    family: str,
    confidence: str,
    rate: int | None,
    workspace: str,
    input_device: str,
    reason: str,
    boundary: str,
    segment: SurveySegment,
    features: dict[str, object],
    evidence: list[str],
    warnings: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "electrical_topology": topology,
        "family": family,
        "confidence": confidence,
        "candidate_bitrate_bps": rate,
        "workspace": workspace,
        "input_device": input_device,
        "reason": reason,
        "boundary": boundary,
        "segment_used": segment.name,
        "sample_interval_us": segment.sample_interval_us,
        "features": features,
        "evidence": evidence,
        "warnings": warnings or [],
    }


def analyze_bus_survey(
    segments: Sequence[SurveySegment],
    *,
    harness: str,
) -> dict[str, object]:
    if harness not in VERIFIED_HARNESSES:
        raise ValueError("a verified harness or protected input selection is required")
    if not segments:
        raise ValueError("bus survey needs at least one capture segment")
    for segment in segments:
        _validate_segment(segment)
    overflow = sorted({name for segment in segments for name in segment.overflow_channels})
    fastest = min(segments, key=lambda item: item.sample_interval_us)
    if overflow:
        return _base_result(
            status="invalid_capture",
            topology="Unresolved",
            family="Unresolved — over-range capture",
            confidence="none",
            rate=None,
            workspace="bus-sniffer",
            input_device="No recommendation",
            reason=f"Input over-range was reported on {', '.join(overflow)}.",
            boundary="Correct the probe, attenuation, range, and connection before collecting more evidence.",
            segment=fastest,
            features={"overflow_channels": overflow},
            evidence=[],
        )

    channel_stats: dict[str, dict[str, float]] = {}
    active_segments: list[SurveySegment] = []
    for segment in sorted(segments, key=lambda item: item.sample_interval_us):
        local_stats = {name: _percentiles(values) for name, values in segment.channels.items()}
        if not channel_stats:
            channel_stats = local_stats
        if any(stats["span_98"] >= 0.12 for stats in local_stats.values()):
            active_segments.append(segment)
    if not active_segments:
        return _base_result(
            status="no_activity",
            topology="Unresolved",
            family="Unresolved — no activity",
            confidence="none",
            rate=None,
            workspace="bus-sniffer",
            input_device="No recommendation",
            reason="No defensible edge or level activity was observed in the bounded survey windows.",
            boundary="A silent request-driven bus may require a known machine event or a longer passive observation.",
            segment=fastest,
            features={"channel_stats": channel_stats},
            evidence=["All observed 98% channel spans were below 0.12 V"],
        )

    segment = active_segments[0]
    stats = {name: _percentiles(values) for name, values in segment.channels.items()}
    can_h_name = "CAN-H" if "CAN-H" in segment.channels else "B"
    can_l_name = "CAN-L" if "CAN-L" in segment.channels else "C"
    differential_available = (
        harness in {"can-network", "protected-differential"}
        and can_h_name in segment.channels
        and can_l_name in segment.channels
    )
    if differential_available:
        positive = segment.channels[can_h_name]
        negative = segment.channels[can_l_name]
        differential = positive - negative
        common_mode = (positive + negative) / 2.0
        correlation = float(np.corrcoef(positive, negative)[0, 1])
        if not np.isfinite(correlation):
            correlation = 0.0
        diff_stats = _percentiles(differential)
        common_stats = _percentiles(common_mode)
        features: dict[str, object] = {
            "channel_stats": stats,
            "differential": diff_stats,
            "common_mode": common_stats,
            "pair_correlation": correlation,
            "rail_fraction": _rail_fraction(differential),
        }
        unsafe_common_windows: list[dict[str, object]] = []
        for item in active_segments:
            positive_name = "CAN-H" if "CAN-H" in item.channels else "B"
            negative_name = "CAN-L" if "CAN-L" in item.channels else "C"
            if positive_name not in item.channels or negative_name not in item.channels:
                continue
            item_common = (
                item.channels[positive_name] + item.channels[negative_name]
            ) / 2.0
            item_common_stats = _percentiles(item_common)
            if item_common_stats["min"] < -16.0 or item_common_stats["max"] > 16.0:
                unsafe_common_windows.append({
                    "segment": item.name,
                    "common_mode": item_common_stats,
                })
        if unsafe_common_windows:
            features["unsafe_common_windows"] = unsafe_common_windows
            return _base_result(
                status="unsafe",
                topology="Differential candidate outside protected common-mode boundary",
                family="Unknown",
                confidence="none",
                rate=None,
                workspace="bus-sniffer",
                input_device="Isolated, appropriately rated differential instrumentation",
                reason="Observed common mode exceeded the software survey boundary in at least one window.",
                boundary="Do not continue direct common-ground capture until the electrical reference and probe ratings are proven safe.",
                segment=segment,
                features=features,
                evidence=[
                    f"unsafe common mode in {len(unsafe_common_windows)} window(s)"
                ],
            )
        can_polarity = "expected"
        try:
            can = analyze_can_waveform(segment.time_us, positive, negative)
        except ValueError:
            can = {"status": "insufficient_timing_evidence"}
        if int(can.get("crc_valid_header_count", 0)) == 0:
            try:
                reversed_can = analyze_can_waveform(
                    segment.time_us, negative, positive
                )
            except ValueError:
                reversed_can = {"status": "insufficient_timing_evidence"}
            if int(reversed_can.get("crc_valid_header_count", 0)) > 0:
                can = reversed_can
                can_polarity = "reversed"
        if can.get("status") == "analyzed" and int(can.get("crc_valid_header_count", 0)) > 0:
            rate = int(can["nominal_bitrate_bps"])
            features["can_analysis"] = can
            features["can_polarity"] = can_polarity
            can_warnings = list(can.get("warnings", []))
            if can_polarity == "reversed":
                can_warnings.append(
                    "CAN polarity is reversed relative to the recorded CAN-H/CAN-L labels; verify or swap the B/C probe leads."
                )
            return _base_result(
                status="classified",
                topology="Differential pair",
                family="CAN-family",
                confidence="medium",
                rate=rate,
                workspace="can",
                input_device="PicoScope 2406B with the commissioned CAN harness",
                reason=f"Differential CAN physical layer with {can['crc_valid_header_count']} CRC-valid frame(s).",
                boundary="The CAN workspace may fingerprint a higher layer only from validated identifiers and frames.",
                segment=segment,
                features=features,
                evidence=[
                    f"{can['crc_valid_header_count']} CRC-valid Classical CAN frame header(s)",
                    f"{rate} bit/s nominal timing",
                    f"pair correlation {correlation:.3f}",
                ],
                warnings=can_warnings,
            )
        def differential_signal(item: SurveySegment):
            positive_name = "CAN-H" if "CAN-H" in item.channels else "B"
            negative_name = "CAN-L" if "CAN-L" in item.channels else "C"
            if positive_name not in item.channels or negative_name not in item.channels:
                return None
            return item.channels[positive_name] - item.channels[negative_name]

        rate, uart = _best_uart_candidate(
            segments, signal_for_segment=differential_signal
        )
        features["uart_validation"] = uart
        bipolar = diff_stats["q05"] < -0.5 and diff_stats["q95"] > 0.5
        digital = _rail_fraction(differential) >= 0.65
        if bipolar and digital and correlation <= -0.5 and int(uart["valid_windows"]) >= 2:
            return _base_result(
                status="classified",
                topology="Differential pair",
                family="RS-485/422-like balanced UART",
                confidence="medium",
                rate=rate,
                workspace="bus-sniffer",
                input_device="isolated RS-485 receive adapter; not the SEL C662",
                reason="Bipolar anti-correlated differential states and repeatable UART framing were observed in multiple windows.",
                boundary="Electrical layer and candidate timing only; application protocol unresolved until bytes and checksums are validated.",
                segment=segment,
                features=features,
                evidence=[
                    f"differential 5–95% range {diff_stats['q05']:.2f} to {diff_stats['q95']:.2f} V",
                    f"pair correlation {correlation:.3f}",
                    f"UART framing valid in {uart['valid_windows']} windows",
                    *( [f"candidate {rate} bit/s"] if rate else [] ),
                ],
                warnings=["Use an isolated RS-485 receiver before byte-level acquisition."],
            )
        return _base_result(
            status="ambiguous",
            topology="Differential pair",
            family="Differential digital or bus candidate — unresolved",
            confidence="low",
            rate=rate,
            workspace="scope",
            input_device="PicoScope 2406B with the selected protected differential front end",
            reason="Differential activity exists, but it did not satisfy validated CAN or repeatable UART framing rules.",
            boundary="Do not choose a protocol adapter from differential voltage shape alone.",
            segment=segment,
            features=features,
            evidence=[f"pair correlation {correlation:.3f}"],
        )

    def single_signal(item: SurveySegment):
        if "A" in item.channels:
            return item.channels["A"]
        return next(iter(item.channels.values()), None)

    rate, uart = _best_uart_candidate(segments, signal_for_segment=single_signal)
    valid_names = {
        str(window["segment"])
        for window in uart["windows"]
        if window.get("valid")
    }
    if valid_names:
        segment = min(
            (item for item in segments if item.name in valid_names),
            key=lambda item: item.sample_interval_us,
        )
    signal_name = "A" if "A" in segment.channels else next(iter(segment.channels))
    signal = segment.channels[signal_name]
    stats = {name: _percentiles(values) for name, values in segment.channels.items()}
    signal_stats = _percentiles(signal)
    rail_fraction = _rail_fraction(signal)
    features = {
        "channel_stats": stats,
        "signal": signal_stats,
        "rail_fraction": rail_fraction,
        "uart_validation": uart,
    }
    framed_uart = int(uart["valid_windows"]) >= 2
    digital = rail_fraction >= 0.65 and rate is not None
    low, high = signal_stats["q05"], signal_stats["q95"]
    if digital and framed_uart and low < -2.5 and high > 2.5:
        return _base_result(
            status="classified",
            topology="Single-ended bipolar",
            family="RS-232 asynchronous candidate",
            confidence="medium",
            rate=rate,
            workspace="bus-sniffer",
            input_device="Isolated RS-232 receiver; SEL C662 only after verified electrical role and pinout",
            reason="Bipolar single-ended levels and repeatable UART framing were observed in multiple windows.",
            boundary="Candidate framing is not device identity; the C662 is not assumed compatible from voltage alone.",
            segment=segment,
            features=features,
            evidence=[
                f"5–95% range {low:.2f} to {high:.2f} V",
                f"candidate {rate} bit/s",
                f"UART framing valid in {uart['valid_windows']} windows",
            ],
        )
    if digital and framed_uart and -0.8 <= low <= 1.2 and 2.0 <= high <= 6.0:
        return _base_result(
            status="classified",
            topology="Single-ended logic",
            family="TTL UART asynchronous candidate",
            confidence="medium",
            rate=rate,
            workspace="bus-sniffer",
            input_device="Protected TTL UART receive adapter matched to the observed logic level",
            reason="Two-rail low-voltage logic and repeatable UART framing were observed in multiple windows.",
            boundary="Voltage and timing do not establish pinout, framing, or application protocol.",
            segment=segment,
            features=features,
            evidence=[
                f"5–95% range {low:.2f} to {high:.2f} V",
                f"candidate {rate} bit/s",
                f"UART framing valid in {uart['valid_windows']} windows",
            ],
        )
    if digital and -1.0 <= low <= 3.0 and 7.0 <= high <= 18.0:
        return _base_result(
            status="classified",
            topology="Single-wire voltage signaling",
            family="12 V single-wire digital candidate",
            confidence="low",
            rate=rate,
            workspace="scope",
            input_device="No protocol adapter recommendation until LIN/K-Line/PWM evidence is validated",
            reason="A 12 V-class two-state signal with repeatable edge timing was observed.",
            boundary="LIN / K-Line / PWM unresolved until framing, checksums, and physical context are validated.",
            segment=segment,
            features=features,
            evidence=[f"5–95% range {low:.2f} to {high:.2f} V", f"candidate {rate} bit/s"],
        )
    return _base_result(
        status="ambiguous",
        topology="Single-ended",
        family="General analog or unresolved signal",
        confidence="low",
        rate=None,
        workspace="scope",
        input_device="PicoScope 2406B with the selected protected probe",
        reason="Activity was observed without defensible two-rail asynchronous bus evidence.",
        boundary="Remain in Scope until electrical behavior and connection context justify a protocol interface.",
        segment=segment,
        features=features,
        evidence=[
            f"5–95% range {low:.2f} to {high:.2f} V",
            f"rail occupancy {rail_fraction * 100.0:.1f}%",
        ],
    )
