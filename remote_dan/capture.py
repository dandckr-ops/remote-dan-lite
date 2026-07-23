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

from remote_dan.can_analysis import analyze_can_waveform, can_crc15_bits

if TYPE_CHECKING:
    from remote_dan.database import EvidenceDatabase

CaptureMode = Literal["auto", "hardware", "simulator"]
Coupling = Literal["AC", "DC"]


@dataclass(frozen=True)
class CapturePreset:
    name: str
    samples: int
    sample_interval_us: float

    @property
    def duration_ms(self) -> float:
        return ((self.samples - 1) * self.sample_interval_us) / 1000.0


@dataclass(frozen=True)
class ScopeChannelConfig:
    channel: Literal["A", "B", "C", "D"]
    enabled: bool
    label: str
    input_range_v: float
    attenuation: float = 1.0
    coupling: Coupling = "DC"

    @property
    def external_range_v(self) -> float:
        return self.input_range_v * self.attenuation


@dataclass(frozen=True)
class ScopeProfile:
    name: str
    label: str
    preset: str
    channels: tuple[ScopeChannelConfig, ...]
    description: str
    warning: str = ""


INPUT_RANGES_V = (0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)
ATTENUATIONS = (1.0, 10.0, 20.0)
COUPLINGS: tuple[Coupling, ...] = ("DC", "AC")


PRESETS = {
    "short": CapturePreset("short", samples=20_000, sample_interval_us=2),
    "medium": CapturePreset("medium", samples=100_000, sample_interval_us=2),
    "long": CapturePreset("long", samples=250_000, sample_interval_us=2),
    "1s": CapturePreset("1s", samples=200_000, sample_interval_us=5),
    "2s": CapturePreset("2s", samples=200_000, sample_interval_us=10),
    "5s": CapturePreset("5s", samples=250_000, sample_interval_us=20),
    "10s": CapturePreset("10s", samples=250_000, sample_interval_us=40),
}
CAN_ANALYSIS_PRESET = CapturePreset(
    "can-analysis",
    samples=250_000,
    sample_interval_us=0.1,
)


def _scope_channels(
    a: ScopeChannelConfig,
    b: ScopeChannelConfig | None = None,
) -> tuple[ScopeChannelConfig, ...]:
    return (
        a,
        b or ScopeChannelConfig("B", False, "Channel B", 20.0),
        ScopeChannelConfig("C", False, "Channel C", 20.0),
        ScopeChannelConfig("D", False, "Channel D", 20.0),
    )


SCOPE_PROFILES = {
    "general": ScopeProfile(
        name="general",
        label="General / custom",
        preset="medium",
        channels=_scope_channels(
            ScopeChannelConfig("A", True, "Channel A", 20.0),
            ScopeChannelConfig("B", True, "Channel B", 20.0),
        ),
        description="Safe high-range starting point for unknown low-voltage signals.",
    ),
    "secondary-ignition": ScopeProfile(
        name="secondary-ignition",
        label="Secondary ignition",
        preset="medium",
        channels=_scope_channels(
            ScopeChannelConfig("A", True, "Secondary pickup", 20.0, coupling="AC")
        ),
        description="Fast ignition-event capture through an approved capacitive pickup.",
        warning=(
            "Use an approved capacitive secondary-ignition pickup. Never connect the "
            "scope, BNC, or ground lead directly to secondary ignition voltage."
        ),
    ),
    "crankshaft-vr": ScopeProfile(
        name="crankshaft-vr",
        label="Crankshaft · VR",
        preset="2s",
        channels=_scope_channels(
            ScopeChannelConfig("A", True, "Crankshaft VR", 20.0, coupling="AC"),
            ScopeChannelConfig("B", False, "Cam / sync", 20.0),
        ),
        description="Bipolar variable-reluctance crank signal with optional sync channel.",
    ),
    "crankshaft-hall": ScopeProfile(
        name="crankshaft-hall",
        label="Crankshaft · Hall",
        preset="2s",
        channels=_scope_channels(
            ScopeChannelConfig("A", True, "Crankshaft Hall", 10.0),
            ScopeChannelConfig("B", False, "Cam / sync", 10.0),
        ),
        description="DC-coupled digital crank signal with optional cam or #1 sync.",
    ),
    "injector-primary": ScopeProfile(
        name="injector-primary",
        label="Injector primary",
        preset="long",
        channels=_scope_channels(
            ScopeChannelConfig("A", True, "Injector primary", 20.0, attenuation=20.0),
            ScopeChannelConfig("B", False, "Current / sync", 20.0),
        ),
        description="Primary-side injector voltage using a properly rated 20:1 attenuator.",
        warning="Confirm the attenuator voltage and category rating before connection.",
    ),
}


def resolve_scope_profile(name: str) -> ScopeProfile:
    try:
        return SCOPE_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown scope profile: {name}") from exc


NETWORK_CHANNELS = (
    ScopeChannelConfig("A", True, "VBAT", 1.0, attenuation=20.0),
    ScopeChannelConfig("B", True, "CAN-H", 10.0),
    ScopeChannelConfig("C", True, "CAN-L", 10.0),
    ScopeChannelConfig("D", False, "TRIG", 10.0),
)


def scope_config_payload(channels: tuple[ScopeChannelConfig, ...]) -> list[dict[str, object]]:
    return [
        {
            "channel": config.channel,
            "enabled": config.enabled,
            "label": config.label,
            "input_range_v": config.input_range_v,
            "attenuation": config.attenuation,
            "coupling": config.coupling,
            "external_range_v": config.external_range_v,
        }
        for config in channels
    ]


def resolve_preset(name: str) -> CapturePreset:
    if name == CAN_ANALYSIS_PRESET.name:
        return CAN_ANALYSIS_PRESET
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
    profile: str = "network"
    channels: tuple[ScopeChannelConfig, ...] = ()


def resolve_capture_channels(request: CaptureRequest) -> tuple[ScopeChannelConfig, ...]:
    if request.profile == "network":
        if request.channels:
            raise ValueError("the network profile channel map cannot be overridden")
        return NETWORK_CHANNELS

    profile = resolve_scope_profile(request.profile)
    channels = request.channels or profile.channels
    if len(channels) != 4 or {config.channel for config in channels} != {"A", "B", "C", "D"}:
        raise ValueError("scope channels must contain A, B, C, and D once")
    enabled = tuple(config for config in channels if config.enabled)
    if not enabled:
        raise ValueError("at least one scope channel must be enabled")
    if len({config.label for config in enabled}) != len(enabled):
        raise ValueError("enabled scope channel labels must be unique")
    for config in channels:
        if config.input_range_v not in INPUT_RANGES_V:
            raise ValueError(f"unsupported input range: {config.input_range_v}")
        if config.attenuation not in ATTENUATIONS:
            raise ValueError(f"unsupported attenuation: {config.attenuation}")
        if config.coupling not in COUPLINGS:
            raise ValueError(f"unsupported coupling: {config.coupling}")
        if config.enabled and config.attenuation == 20.0 and config.input_range_v > 20.0:
            raise ValueError("20:1 attenuation is limited to a ±400 V external display range")
    return tuple(sorted(channels, key=lambda config: config.channel))


@dataclass(frozen=True)
class CaptureData:
    backend: str
    preset: CapturePreset
    time_us: np.ndarray
    channels: dict[str, np.ndarray]
    profile: str = "network"
    channel_configs: tuple[ScopeChannelConfig, ...] = ()
    overflow_channels: tuple[str, ...] = ()

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(self.channels)


def _sim_bits(value: int, width: int) -> list[int]:
    return [int(bit) for bit in f"{value:0{width}b}"]


def _sim_stuff(bits: list[int]) -> list[int]:
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


def _sim_j1939_frame(identifier: int, payload: bytes) -> list[int]:
    identifier_a = identifier >> 18
    identifier_b = identifier & ((1 << 18) - 1)
    frame_without_crc = (
        [0]
        + _sim_bits(identifier_a, 11)
        + [1, 1]
        + _sim_bits(identifier_b, 18)
        + [0, 0, 0]
        + _sim_bits(len(payload), 4)
        + [bit for value in payload for bit in _sim_bits(value, 8)]
    )
    return _sim_stuff(frame_without_crc + can_crc15_bits(frame_without_crc)) + [1, 0, 1] + [1] * 7 + [1] * 3


def _sim_network_bus(samples: int, sample_interval_us: float) -> np.ndarray:
    samples_per_bit = max(1, round(2.0 / sample_interval_us))
    target_bits = (samples + samples_per_bit - 1) // samples_per_bit
    frames = (
        _sim_j1939_frame(0x18F00401, bytes.fromhex("1122334455667788")),
        _sim_j1939_frame(0x18FEF100, bytes.fromhex("8877665544332211")),
    )
    wire: list[int] = [1] * 20
    frame_index = 0
    while len(wire) < target_bits:
        wire.extend(frames[frame_index % len(frames)])
        wire.extend([1] * 20)
        frame_index += 1
    logical = np.repeat(np.asarray(wire, dtype=np.int8), samples_per_bit)[:samples]
    return (logical == 0).astype(np.float64)


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

        if request.profile == "network":
            vbat = 13.65 + 0.035 * np.sin(2 * np.pi * 120 * time_s)
            vbat += rng.normal(0.0, 0.006, preset.samples)

            bus = _sim_network_bus(preset.samples, preset.sample_interval_us)
            edge_rounding = np.convolve(bus, np.ones(3) / 3.0, mode="same")
            noise = rng.normal(0.0, 0.008, preset.samples)
            channels = {
                "VBAT": vbat,
                "CAN-H": 2.5 + 1.0 * edge_rounding + noise,
                "CAN-L": 2.5 - 1.0 * edge_rounding - noise,
            }
            configs = resolve_capture_channels(request)
        else:
            configs = resolve_capture_channels(request)
            enabled = tuple(config for config in configs if config.enabled)
            channels = self._simulate_scope(request.profile, enabled, time_s, rng)

        return CaptureData(
            backend=self.name,
            preset=preset,
            time_us=time_us,
            channels=channels,
            profile=request.profile,
            channel_configs=configs,
        )

    @staticmethod
    def _simulate_scope(
        profile: str,
        configs: tuple[ScopeChannelConfig, ...],
        time_s: np.ndarray,
        rng: np.random.Generator,
    ) -> dict[str, np.ndarray]:
        channels: dict[str, np.ndarray] = {}
        for index, config in enumerate(configs):
            noise = rng.normal(0.0, max(config.external_range_v * 0.0005, 0.001), time_s.size)
            phase = index * np.pi / 5.0
            if profile == "secondary-ignition":
                period = 0.025
                event_phase = np.mod(time_s + index * 0.004, period)
                firing = np.exp(-event_phase / 0.00035) * min(8.0, config.external_range_v * 0.65)
                burn = ((event_phase > 0.001) & (event_phase < 0.0035)).astype(float) * 1.4
                values = firing + burn + noise
            elif profile == "crankshaft-vr":
                carrier = np.sin(2 * np.pi * 360 * time_s + phase)
                missing_tooth = np.mod(time_s, 1 / 6.0) > 0.004
                values = carrier * missing_tooth * min(18.0, config.external_range_v * 0.45) + noise
            elif profile == "crankshaft-hall":
                square = (np.sin(2 * np.pi * 300 * time_s + phase) >= 0).astype(float)
                missing_tooth = np.mod(time_s, 0.2) > 0.006
                values = square * missing_tooth * min(5.0, config.external_range_v * 0.5) + noise
            elif profile == "injector-primary":
                event_phase = np.mod(time_s + index * 0.01, 0.05)
                values = np.full(time_s.size, 13.8)
                values[(event_phase > 0.008) & (event_phase < 0.012)] = 0.8
                flyback = (event_phase >= 0.012) & (event_phase < 0.013)
                values[flyback] = min(80.0, config.external_range_v * 0.65) * np.exp(
                    -(event_phase[flyback] - 0.012) / 0.00025
                )
                values += noise
            else:
                amplitude = max(config.external_range_v * 0.2, 0.05)
                values = amplitude * np.sin(2 * np.pi * (80 + index * 35) * time_s + phase) + noise
            channels[config.label] = values
        return channels


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
        lock: threading.Lock | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.database = database
        self._lock = lock or threading.Lock()
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
                        "profile": data.profile,
                        "scope_config": scope_config_payload(data.channel_configs),
                    },
                )
            manifest: dict[str, object] = {
                "run_id": run_id,
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "capture",
                "capture_type": request.capture_type,
                "profile": data.profile,
                "preset": data.preset.name,
                "backend": data.backend,
                "samples": data.preset.samples,
                "sample_interval_us": data.preset.sample_interval_us,
                "duration_ms": data.preset.duration_ms,
                "channels": list(data.channel_names),
                "scope_config": scope_config_payload(data.channel_configs),
                "overflow_channels": list(data.overflow_channels),
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
        config_payload = scope_config_payload(data.channel_configs)
        summary: dict[str, object] = {
            "captured_at": captured_at.isoformat(),
            "label": request.label.strip() or "capture",
            "backend": data.backend,
            "capture_type": request.capture_type,
            "profile": data.profile,
            "preset": data.preset.name,
            "samples": data.preset.samples,
            "sample_interval_us": data.preset.sample_interval_us,
            "duration_ms": data.preset.duration_ms,
            "channel_stats": {name: _stats(values) for name, values in data.channels.items()},
            "preview_channels": ["CAN-H", "CAN-L"] if data.profile == "network" else data.channel_names,
            "scope_config": config_payload,
            "overflow_channels": list(data.overflow_channels),
        }

        if data.profile == "network":
            vbat = data.channels["VBAT"]
            can_h = data.channels["CAN-H"]
            can_l = data.channels["CAN-L"]
            differential = can_h - can_l
            common_mode = (can_h + can_l) / 2.0
            can_analysis = analyze_can_waveform(data.time_us, can_h, can_l)
            can_polarity = "expected"
            if int(can_analysis.get("crc_valid_header_count", 0)) == 0:
                reversed_analysis = analyze_can_waveform(data.time_us, can_l, can_h)
                if int(reversed_analysis.get("crc_valid_header_count", 0)) > 0:
                    can_analysis = reversed_analysis
                    can_polarity = "reversed"
                    can_analysis.setdefault("warnings", []).append(
                        "CAN polarity is reversed relative to the recorded CAN-H/CAN-L labels; verify or swap the B/C probe leads."
                    )
            if data.overflow_channels:
                can_analysis["confidence"] = "low"
                can_analysis.setdefault("warnings", []).append(
                    "Pico input overflow was observed; timing and level conclusions are degraded."
                )
            summary.update({
                "differential_b_minus_c": _stats(differential),
                "common_mode": _stats(common_mode),
                "can_h_can_l_correlation": float(np.corrcoef(can_h, can_l)[0, 1]),
                "can_polarity": can_polarity,
                "can_analysis": can_analysis,
            })
            stack = np.column_stack((data.time_us, vbat, can_h, can_l, differential, common_mode))
            header = "time_us,vbat_v,can_h_v,can_l_v,diff_b_minus_c_v,common_mode_v"
        else:
            enabled_configs = {
                config.label: config for config in data.channel_configs if config.enabled
            }
            stack = np.column_stack((data.time_us, *data.channels.values()))
            headers = ["time_us"]
            for label in data.channel_names:
                config = enabled_configs[label]
                safe_label = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "signal"
                headers.append(f"{config.channel.lower()}_{safe_label}_v")
            header = ",".join(headers)

        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        np.savetxt(
            run_dir / "capture.csv",
            stack,
            delimiter=",",
            header=header,
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
        colors = {
            "A": "#FFBC42",
            "B": "#5CFF9A",
            "C": "#65B7FF",
            "D": "#C8A7FF",
            "CAN-H": "#5CFF9A",
            "CAN-L": "#65B7FF",
        }

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
            if data.profile == "network":
                figure, axis = plt.subplots(1, 1, figsize=(12, 5.2), constrained_layout=True)
                for name in ("CAN-H", "CAN-L"):
                    axis.plot(
                        t_ms,
                        data.channels[name][::stride],
                        label=name,
                        color=colors[name],
                        linewidth=1,
                    )
                axis.set_ylabel("BUS / V")
                axis.set_xlabel("TIME / ms")
                axis.legend(loc="upper right", frameon=False)
                axis.grid(True, alpha=0.45)
            else:
                enabled_configs = [
                    config for config in data.channel_configs if config.enabled
                ]
                figure, axes = plt.subplots(
                    len(enabled_configs),
                    1,
                    figsize=(12, max(4.2, 2.5 * len(enabled_configs))),
                    sharex=True,
                    squeeze=False,
                    constrained_layout=True,
                )
                for axis, config in zip(axes[:, 0], enabled_configs, strict=True):
                    axis.plot(
                        t_ms,
                        data.channels[config.label][::stride],
                        color=colors[config.channel],
                        linewidth=1,
                    )
                    axis.set_ylabel(f"{config.channel} · {config.label} / V")
                    axis.set_ylim(-config.external_range_v, config.external_range_v)
                    axis.grid(True, alpha=0.45)
                axes[-1, 0].set_xlabel("TIME / ms")

            figure.suptitle(
                f"FIELD JOURNAL · {request.label.strip() or 'CAPTURE'} · "
                f"{data.profile.upper()} · {data.preset.name.upper()}"
            )
            figure.savefig(png_path, dpi=150, metadata={"Software": "Remote Dan Lite"})
            figure.savefig(pdf_path, format="pdf", metadata={
                "Title": f"Remote Dan Lite capture: {request.label.strip() or 'capture'}",
                "Author": "Field Journal",
                "Subject": "Traceworks diagnostic evidence",
            })
            plt.close(figure)
