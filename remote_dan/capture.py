from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import TYPE_CHECKING, Literal, Protocol

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

if TYPE_CHECKING:
    from remote_dan.database import EvidenceDatabase

CaptureMode = Literal["auto", "hardware", "simulator"]


@dataclass(frozen=True)
class CapturePreset:
    name: str
    samples: int
    sample_interval_us: int

    @property
    def duration_ms(self) -> float:
        return ((self.samples - 1) * self.sample_interval_us) / 1000.0


PRESETS = {
    "short": CapturePreset("short", samples=20_000, sample_interval_us=2),
    "medium": CapturePreset("medium", samples=100_000, sample_interval_us=2),
    "long": CapturePreset("long", samples=250_000, sample_interval_us=2),
}


def resolve_preset(name: str) -> CapturePreset:
    try:
        return PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"unknown preset: {name}") from exc


@dataclass(frozen=True)
class CaptureRequest:
    label: str
    preset: str = "short"
    mode: CaptureMode = "auto"
    capture_type: str = "scope"
    session_id: int | None = None
    test_type: str | None = None


@dataclass(frozen=True)
class CaptureData:
    backend: str
    preset: CapturePreset
    time_us: np.ndarray
    channels: dict[str, np.ndarray]

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(self.channels)


class SimulatorBackend:
    """Deterministic source that resembles battery voltage plus a CAN pair."""

    name = "simulator"

    def __init__(self, seed: int = 2406) -> None:
        self.seed = seed

    def capture(self, request: CaptureRequest) -> CaptureData:
        preset = resolve_preset(request.preset)
        rng = np.random.default_rng(self.seed)
        time_us = np.arange(preset.samples, dtype=np.float64) * preset.sample_interval_us
        time_s = time_us / 1_000_000.0

        vbat = 13.65 + 0.035 * np.sin(2 * np.pi * 120 * time_s)
        vbat += rng.normal(0.0, 0.006, preset.samples)

        samples_per_bit = max(8, int(20 / preset.sample_interval_us))
        bit_count = (preset.samples + samples_per_bit - 1) // samples_per_bit
        dominant = rng.integers(0, 2, bit_count, dtype=np.int8)
        bus = np.repeat(dominant, samples_per_bit)[: preset.samples].astype(np.float64)
        edge_rounding = np.convolve(bus, np.ones(3) / 3.0, mode="same")
        noise = rng.normal(0.0, 0.008, preset.samples)
        can_h = 2.5 + 1.0 * edge_rounding + noise
        can_l = 2.5 - 1.0 * edge_rounding - noise

        return CaptureData(
            backend=self.name,
            preset=preset,
            time_us=time_us,
            channels={"VBAT": vbat, "CAN-H": can_h, "CAN-L": can_l},
        )


class CaptureBackend(Protocol):
    name: str

    def capture(self, request: CaptureRequest) -> CaptureData: ...


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "capture"


def _stats(values: np.ndarray) -> dict[str, float]:
    return {
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
        "p2p": float(np.ptp(values)),
        "std": float(np.std(values)),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CaptureManager:
    def __init__(
        self,
        data_dir: Path,
        backend: CaptureBackend,
        database: EvidenceDatabase | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.database = database
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def run(self, request: CaptureRequest) -> dict[str, object]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a capture is already in progress")
        try:
            return self._run_locked(request)
        finally:
            self._lock.release()

    def _run_locked(self, request: CaptureRequest) -> dict[str, object]:
        data = self.backend.capture(request)
        captured_at = datetime.now(UTC)
        run_id = (
            captured_at.strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{_slugify(request.label)}-{data.preset.name}"
        )
        partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=self.data_dir))
        final = self.data_dir / run_id
        capture_id: int | None = None
        try:
            summary = self._write_evidence(partial, request, data, captured_at)
            artifacts = [
                "capture.csv",
                "manifest.json",
                "overview.png",
                "report.pdf",
                "summary.json",
            ]
            sha256 = {
                name: _sha256(partial / name)
                for name in artifacts
                if name != "manifest.json"
            }
            if self.database is not None:
                capture_id = self.database.create_capture(
                    session_id=request.session_id,
                    run_id=run_id,
                    captured_at=captured_at.isoformat(),
                    capture_type=request.capture_type,
                    test_type=request.test_type,
                    label=request.label.strip() or "capture",
                    backend=data.backend,
                    preset=data.preset.name,
                    samples=data.preset.samples,
                    sample_interval_us=data.preset.sample_interval_us,
                    duration_ms=data.preset.duration_ms,
                    metadata={
                        "channels": list(data.channel_names),
                        "summary": summary,
                    },
                )
            manifest: dict[str, object] = {
                "run_id": run_id,
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "capture",
                "preset": data.preset.name,
                "backend": data.backend,
                "samples": data.preset.samples,
                "sample_interval_us": data.preset.sample_interval_us,
                "duration_ms": data.preset.duration_ms,
                "channels": list(data.channel_names),
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
                artifact_metadata = {
                    "capture.csv": ("raw_waveform", "text/csv"),
                    "manifest.json": ("manifest", "application/json"),
                    "overview.png": ("preview", "image/png"),
                    "report.pdf": ("report", "application/pdf"),
                    "summary.json": ("summary", "application/json"),
                }
                for name in artifacts:
                    path = final / name
                    kind, media_type = artifact_metadata[name]
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

    def _write_evidence(
        self,
        run_dir: Path,
        request: CaptureRequest,
        data: CaptureData,
        captured_at: datetime,
    ) -> dict[str, object]:
        vbat = data.channels["VBAT"]
        can_h = data.channels["CAN-H"]
        can_l = data.channels["CAN-L"]
        differential = can_h - can_l
        common_mode = (can_h + can_l) / 2.0
        summary: dict[str, object] = {
            "captured_at": captured_at.isoformat(),
            "label": request.label.strip() or "capture",
            "backend": data.backend,
            "preset": data.preset.name,
            "samples": data.preset.samples,
            "sample_interval_us": data.preset.sample_interval_us,
            "duration_ms": data.preset.duration_ms,
            "channel_stats": {name: _stats(values) for name, values in data.channels.items()},
            "differential_b_minus_c": _stats(differential),
            "common_mode": _stats(common_mode),
            "can_h_can_l_correlation": float(np.corrcoef(can_h, can_l)[0, 1]),
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        stack = np.column_stack((data.time_us, vbat, can_h, can_l, differential, common_mode))
        np.savetxt(
            run_dir / "capture.csv",
            stack,
            delimiter=",",
            header="time_us,vbat_v,can_h_v,can_l_v,diff_b_minus_c_v,common_mode_v",
            comments="",
            fmt="%.8g",
        )

        self._write_plot(run_dir / "overview.png", run_dir / "report.pdf", request, data)
        return summary

    @staticmethod
    def _write_plot(
        png_path: Path,
        pdf_path: Path,
        request: CaptureRequest,
        data: CaptureData,
    ) -> None:
        max_points = 5_000
        stride = max(1, data.preset.samples // max_points)
        t_ms = data.time_us[::stride] / 1000.0
        colors = {"VBAT": "#FFBC42", "CAN-H": "#5CFF9A", "CAN-L": "#65B7FF"}

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
            figure, axes = plt.subplots(2, 1, figsize=(12, 7.2), sharex=True, constrained_layout=True)
            axes[0].plot(t_ms, data.channels["VBAT"][::stride], color=colors["VBAT"], linewidth=1)
            axes[0].set_ylabel("VBAT / V")
            for name in ("CAN-H", "CAN-L"):
                axes[1].plot(t_ms, data.channels[name][::stride], label=name, color=colors[name], linewidth=1)
            axes[1].set_ylabel("BUS / V")
            axes[1].set_xlabel("TIME / ms")
            axes[1].legend(loc="upper right", frameon=False)
            for axis in axes:
                axis.grid(True, alpha=0.45)
            figure.suptitle(f"FIELD JOURNAL · {request.label.strip() or 'CAPTURE'} · {data.preset.name.upper()}")
            figure.savefig(png_path, dpi=150, metadata={"Software": "Remote Dan Lite"})
            figure.savefig(pdf_path, format="pdf", metadata={
                "Title": f"Remote Dan Lite capture: {request.label.strip() or 'capture'}",
                "Author": "Field Journal",
                "Subject": "Traceworks diagnostic evidence",
            })
            plt.close(figure)
