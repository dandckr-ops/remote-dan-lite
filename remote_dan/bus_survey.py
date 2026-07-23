from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Protocol

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from remote_dan.bus_sniffer import SurveySegment, VERIFIED_HARNESSES, analyze_bus_survey
from remote_dan.capture import (
    CaptureBackend,
    CaptureRequest,
    ScopeChannelConfig,
    SimulatorBackend,
    resolve_preset,
)
from remote_dan.database import EvidenceDatabase


SURVEY_PRESETS = (
    ("fast", "can-analysis"),
    ("context", "medium"),
    ("sparse", "2s"),
)


@dataclass(frozen=True)
class BusSurveyRequest:
    label: str
    harness: str
    mode: str = "simulator"
    session_id: int | None = None
    low_voltage_confirmed: bool = False
    common_reference_confirmed: bool = False
    probe_rating_confirmed: bool = False
    passive_only_confirmed: bool = False


@dataclass(frozen=True)
class BusSurveyData:
    backend: str
    segments: tuple[SurveySegment, ...]
    provenance: dict[str, object]


def _safety_attestations(request: BusSurveyRequest) -> dict[str, bool]:
    return {
        "low_voltage_domain": request.low_voltage_confirmed,
        "common_reference": request.common_reference_confirmed,
        "probe_rating_and_attenuation": request.probe_rating_confirmed,
        "passive_inputs_only": request.passive_only_confirmed,
    }


class BusSurveyBackend(Protocol):
    name: str

    def collect(self, request: BusSurveyRequest) -> BusSurveyData: ...


def _uart_pattern(payload: bytes) -> np.ndarray:
    bits: list[int] = [1] * 12
    for value in payload:
        bits.append(0)
        bits.extend((value >> index) & 1 for index in range(8))
        bits.extend((1, 1))
    bits.extend([1] * 12)
    return np.asarray(bits, dtype=np.float64)


def _sim_uart_segment(
    name: str,
    preset_name: str,
    *,
    differential: bool,
) -> SurveySegment:
    preset = resolve_preset(preset_name)
    pattern = _uart_pattern(bytes.fromhex("010300000002c40b") * 3)
    bit_period_us = 1_000_000.0 / 9600.0
    bit_indices = np.floor(
        np.arange(preset.samples, dtype=np.float64)
        * preset.sample_interval_us
        / bit_period_us
    ).astype(np.int64)
    logic = pattern[bit_indices % pattern.size]
    time_us = np.arange(preset.samples, dtype=np.float64) * preset.sample_interval_us
    if differential:
        state = np.where(logic > 0.5, 1.5, -1.5)
        channels = {"B": 2.5 + state, "C": 2.5 - state}
    else:
        channels = {"A": logic * 5.0}
    return SurveySegment(name=name, time_us=time_us, channels=channels)


class BusSurveySimulatorBackend:
    name = "bus-survey-simulator"

    def __init__(self, *, seed: int = 2406) -> None:
        self._scope = SimulatorBackend(seed=seed)

    def collect(self, request: BusSurveyRequest) -> BusSurveyData:
        if request.harness not in VERIFIED_HARNESSES:
            raise ValueError("a verified harness or protected input selection is required")
        segments: list[SurveySegment] = []
        provenance: list[dict[str, object]] = []
        for segment_name, preset_name in SURVEY_PRESETS:
            started_at = datetime.now(UTC).isoformat()
            if request.harness == "can-network":
                captured = self._scope.capture(CaptureRequest(
                    label=request.label,
                    preset=preset_name,
                    mode="simulator",
                    capture_type="bus_survey",
                    profile="network",
                ))
                segment = SurveySegment(
                    name=segment_name,
                    time_us=captured.time_us,
                    channels=captured.channels,
                    overflow_channels=captured.overflow_channels,
                )
                channel_configs = [asdict(config) for config in captured.channel_configs]
            else:
                segment = _sim_uart_segment(
                    segment_name,
                    preset_name,
                    differential=request.harness == "protected-differential",
                )
                channel_configs = [
                    asdict(config)
                    for config in _survey_channels(request.harness)
                    if config.enabled
                ]
            segments.append(segment)
            provenance.append({
                "name": segment_name,
                "preset": preset_name,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "requested_sample_interval_us": resolve_preset(preset_name).sample_interval_us,
                "actual_sample_interval_us": segment.sample_interval_us,
                "samples": int(segment.time_us.size),
                "channel_configs": channel_configs,
            })
        return BusSurveyData(
            backend=self.name,
            segments=tuple(segments),
            provenance={"mode": "simulator", "segments": provenance},
        )


def _survey_channels(harness: str) -> tuple[ScopeChannelConfig, ...]:
    if harness == "protected-differential":
        return (
            ScopeChannelConfig("A", False, "A", 20.0),
            ScopeChannelConfig("B", True, "B", 20.0),
            ScopeChannelConfig("C", True, "C", 20.0),
            ScopeChannelConfig("D", False, "D", 20.0),
        )
    if harness == "protected-single-ended":
        return (
            ScopeChannelConfig("A", True, "A", 20.0),
            ScopeChannelConfig("B", False, "B", 20.0),
            ScopeChannelConfig("C", False, "C", 20.0),
            ScopeChannelConfig("D", False, "D", 20.0),
        )
    return ()


class PicoBusSurveyBackend:
    name = "pico-bus-survey"

    def __init__(self, capture_backend: CaptureBackend) -> None:
        self.capture_backend = capture_backend

    def collect(self, request: BusSurveyRequest) -> BusSurveyData:
        if request.harness not in VERIFIED_HARNESSES:
            raise ValueError("a verified harness or protected input selection is required")
        channels = _survey_channels(request.harness)
        profile = "network" if request.harness == "can-network" else "general"
        segments: list[SurveySegment] = []
        provenance: list[dict[str, object]] = []
        for segment_name, preset_name in SURVEY_PRESETS:
            started_at = datetime.now(UTC).isoformat()
            captured = self.capture_backend.capture(CaptureRequest(
                label=request.label,
                preset=preset_name,
                mode="hardware",
                capture_type="bus_survey",
                profile=profile,
                channels=channels,
            ))
            segment = SurveySegment(
                name=segment_name,
                time_us=captured.time_us,
                channels=captured.channels,
                overflow_channels=captured.overflow_channels,
            )
            segments.append(segment)
            provenance.append({
                "name": segment_name,
                "preset": preset_name,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "requested_sample_interval_us": resolve_preset(preset_name).sample_interval_us,
                "actual_sample_interval_us": segment.sample_interval_us,
                "samples": int(segment.time_us.size),
                "channel_configs": [asdict(config) for config in captured.channel_configs],
            })
        return BusSurveyData(
            backend=self.name,
            segments=tuple(segments),
            provenance={"mode": "hardware", "segments": provenance},
        )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "bus-survey"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class BusSurveyManager:
    def __init__(
        self,
        data_dir: Path,
        backend: BusSurveyBackend,
        database: EvidenceDatabase | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.database = database
        self._lock = lock or threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def run(self, request: BusSurveyRequest) -> dict[str, object]:
        if request.harness not in VERIFIED_HARNESSES:
            raise ValueError("a verified harness or protected input selection is required")
        attestations = _safety_attestations(request)
        if request.mode == "hardware" and not all(attestations.values()):
            raise ValueError(
                "hardware bus survey requires all low-voltage, common-reference, "
                "probe-rating, and passive-only safety attestations"
            )
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("the PicoScope is already in use")
        try:
            data = self.backend.collect(request)
        finally:
            self._lock.release()
        return self._package(request, data)

    def _package(
        self,
        request: BusSurveyRequest,
        data: BusSurveyData,
    ) -> dict[str, object]:
        attestations = _safety_attestations(request)
        classification = analyze_bus_survey(data.segments, harness=request.harness)
        captured_at = datetime.now(UTC)
        run_id = (
            captured_at.strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{_slugify(request.label)}-bus-survey"
        )
        partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=self.data_dir))
        final = self.data_dir / run_id
        capture_id: int | None = None
        try:
            segment_metadata = [
                {
                    "name": segment.name,
                    "samples": int(segment.time_us.size),
                    "sample_interval_us": segment.sample_interval_us,
                    "duration_ms": (
                        (segment.time_us.size - 1) * segment.sample_interval_us / 1000.0
                    ),
                    "channels": list(segment.channels),
                    "overflow_channels": list(segment.overflow_channels),
                }
                for segment in data.segments
            ]
            summary: dict[str, object] = {
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "bus survey",
                "backend": data.backend,
                "capture_type": "bus_survey",
                "profile": "bus-sniffer",
                "physical_connection_required": request.harness,
                "safety_attestations": attestations,
                "classifier_version": 1,
                "segments": segment_metadata,
                "acquisition_provenance": data.provenance,
                "classification": classification,
                "writes_performed": 0,
                "bounded_capture_strategy": [
                    "fast 25 ms / 10 MS/s class window",
                    "context 200 ms / 500 kS/s window",
                    "sparse 2 s / 100 kS/s window",
                ],
            }
            csv_names: list[str] = []
            for segment in data.segments:
                name = f"{segment.name}.csv"
                csv_names.append(name)
                columns = [segment.time_us] + [
                    segment.channels[channel] for channel in segment.channels
                ]
                np.savetxt(
                    partial / name,
                    np.column_stack(columns),
                    delimiter=",",
                    header=",".join(["time_us", *segment.channels]),
                    comments="",
                    fmt="%.9g",
                )
            (partial / "segments.json").write_text(
                json.dumps(segment_metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (partial / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self._write_plot(
                partial / "overview.png",
                partial / "report.pdf",
                request,
                data,
                classification,
            )
            artifacts = [
                *csv_names,
                "segments.json",
                "overview.png",
                "report.pdf",
                "summary.json",
                "manifest.json",
            ]
            sha256 = {
                name: _sha256(partial / name)
                for name in artifacts
                if name != "manifest.json"
            }
            total_samples = sum(segment.time_us.size for segment in data.segments)
            total_duration_ms = sum(item["duration_ms"] for item in segment_metadata)
            if self.database is not None:
                capture_id = self.database.create_capture(
                    session_id=request.session_id,
                    run_id=run_id,
                    captured_at=captured_at.isoformat(),
                    capture_type="bus_survey",
                    label=request.label.strip() or "bus survey",
                    backend=data.backend,
                    preset="fast+context+sparse",
                    samples=total_samples,
                    sample_interval_us=min(
                        segment.sample_interval_us for segment in data.segments
                    ),
                    duration_ms=total_duration_ms,
                    metadata={"profile": "bus-sniffer", "summary": summary},
                )
            manifest: dict[str, object] = {
                "run_id": run_id,
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "bus survey",
                "capture_type": "bus_survey",
                "profile": "bus-sniffer",
                "preset": "fast+context+sparse",
                "backend": data.backend,
                "samples": total_samples,
                "sample_interval_us": min(
                    segment.sample_interval_us for segment in data.segments
                ),
                "duration_ms": total_duration_ms,
                "channels": sorted({
                    channel for segment in data.segments for channel in segment.channels
                }),
                "artifacts": artifacts,
                "sha256": sha256,
                "summary": summary,
            }
            if capture_id is not None:
                manifest["capture_id"] = capture_id
            (partial / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            partial.rename(final)
            if self.database is not None and capture_id is not None:
                media = {
                    **{name: ("survey_waveform", "text/csv") for name in csv_names},
                    "segments.json": ("survey_segments", "application/json"),
                    "overview.png": ("preview", "image/png"),
                    "report.pdf": ("report", "application/pdf"),
                    "summary.json": ("summary", "application/json"),
                    "manifest.json": ("manifest", "application/json"),
                }
                for name in artifacts:
                    path = final / name
                    kind, media_type = media[name]
                    self.database.add_artifact(
                        capture_id=capture_id,
                        kind=kind,
                        filename=name,
                        relative_path=f"{run_id}/{name}",
                        media_type=media_type,
                        size_bytes=path.stat().st_size,
                        sha256=_sha256(path),
                    )
                self.database.set_capture_status(capture_id, "complete")
            return manifest
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            if self.database is not None and capture_id is not None:
                self.database.delete_capture(capture_id)
            raise

    @staticmethod
    def _write_plot(
        png_path: Path,
        pdf_path: Path,
        request: BusSurveyRequest,
        data: BusSurveyData,
        classification: dict[str, object],
    ) -> None:
        with plt.rc_context({
            "figure.facecolor": "#07100D",
            "axes.facecolor": "#0C1914",
            "axes.edgecolor": "#284136",
            "axes.labelcolor": "#E9EEE9",
            "xtick.color": "#8CA095",
            "ytick.color": "#8CA095",
            "text.color": "#E9EEE9",
            "grid.color": "#284136",
            "font.family": "DejaVu Sans",
        }):
            figure, axes = plt.subplots(
                len(data.segments), 1, figsize=(12, 8.5), constrained_layout=True
            )
            axes = np.atleast_1d(axes)
            colors = ("#FFBD4A", "#5CFF9A", "#65B8FF", "#C8A7FF")
            for axis, segment in zip(axes, data.segments, strict=True):
                stride = max(1, segment.time_us.size // 6000)
                time_ms = segment.time_us[::stride] / 1000.0
                for color, (channel, values) in zip(colors, segment.channels.items()):
                    axis.plot(time_ms, values[::stride], color=color, linewidth=0.8, label=channel)
                axis.set_title(
                    f"{segment.name.upper()} · {segment.sample_interval_us:g} µs/sample · {segment.time_us.size:,} samples",
                    loc="left",
                    fontsize=10,
                )
                axis.set_xlabel("TIME / ms")
                axis.set_ylabel("V")
                axis.grid(True, alpha=0.4)
                axis.legend(loc="upper right", fontsize=8)
            evidence_class = "SIMULATED EVIDENCE · " if request.mode == "simulator" else ""
            figure.suptitle(
                f"{evidence_class}FIELD JOURNAL · {request.label.strip() or 'BUS SURVEY'} · {classification['family']} · {classification['confidence'].upper()} CONFIDENCE"
            )
            figure.savefig(png_path, dpi=150, metadata={"Software": "Remote Dan Lite"})
            figure.savefig(pdf_path, format="pdf", metadata={
                "Title": f"Remote Dan Lite bus survey: {request.label}",
                "Author": "Field Journal",
                "Subject": "Passive multi-window electrical bus classification evidence",
            })
            plt.close(figure)
