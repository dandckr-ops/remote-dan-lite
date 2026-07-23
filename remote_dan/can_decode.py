from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any

import numpy as np

from remote_dan.can_analysis import aggregate_can_identifiers, decode_can_waveform
from remote_dan.database import EvidenceDatabase

DECODER_VERSION = 1
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_SAMPLES = 1_000_000
MAX_SAMPLE_INTERVAL_US = 0.25
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
LIMITATIONS = [
    "Generic Classical CAN frame decoding only.",
    "No DBC, OEM signal meaning, PID interpretation, VIN extraction, ISO-TP, UDS, or CAN FD payload decoding.",
    "Passive source inspection only; no CAN writes, ACK generation, replay, stimulation, or queries are implemented.",
]


@dataclass(frozen=True)
class CanDecodeRequest:
    source_run_id: str
    label: str = "CAN decode"


class CanDecodeSourceNotFound(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "can-decode"


def _confined(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("source path escapes the capture root")
    return resolved


def _spreadsheet_safe(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def eligible_bus_survey_classification(classification: object) -> bool:
    if not isinstance(classification, dict):
        return False
    if (
        classification.get("family") == "CAN-family"
        or classification.get("workspace") == "can"
    ):
        return True
    return (
        classification.get("status") == "ambiguous"
        and classification.get("topology") == "Differential pair"
    )


class CanDecodeManager:
    """Create immutable child evidence from existing sampled CAN waveforms."""

    def __init__(
        self,
        data_dir: Path,
        *,
        database: EvidenceDatabase | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.database = database
        self._lock = lock or threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def run(self, request: CanDecodeRequest) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a CAN decode is already in progress")
        try:
            return self._run_locked(request)
        finally:
            self._lock.release()

    def _run_locked(self, request: CanDecodeRequest) -> dict[str, Any]:
        if (
            not RUN_ID_PATTERN.fullmatch(request.source_run_id)
            or request.source_run_id in {".", ".."}
            or ".partial" in request.source_run_id.lower()
        ):
            raise ValueError("invalid source run ID")
        label = request.label.strip()
        if not label or len(label) > 80:
            raise ValueError("CAN decode label must contain 1 to 80 characters")

        source_dir = self.data_dir / request.source_run_id
        manifest_path = source_dir / "manifest.json"
        if source_dir.is_symlink():
            raise ValueError("source directory symlink escapes are not allowed")
        try:
            _confined(source_dir, self.data_dir)
            _confined(manifest_path, self.data_dir)
        except FileNotFoundError as exc:
            raise CanDecodeSourceNotFound("source capture not found") from exc
        try:
            source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("source manifest is missing or malformed") from exc
        if source_manifest.get("run_id") != request.source_run_id:
            raise ValueError("source manifest run ID does not match its directory")

        capture_type = source_manifest.get("capture_type")
        profile = source_manifest.get("profile")
        if capture_type == "bus_survey":
            classification = source_manifest.get("summary", {}).get("classification", {})
            if not eligible_bus_survey_classification(classification):
                raise ValueError("bus survey is not classified as a CAN candidate")
            source_artifact = "fast.csv"
        elif capture_type == "can" or (capture_type == "scope" and profile == "network"):
            source_artifact = "capture.csv"
        else:
            raise ValueError("source capture is not an eligible network or CAN bus survey capture")

        source_path = source_dir / source_artifact
        if source_path.is_symlink():
            raise ValueError("source artifact symlinks are not allowed")
        try:
            confined_source = _confined(source_path, self.data_dir)
        except FileNotFoundError as exc:
            raise ValueError(f"source artifact {source_artifact} is missing") from exc
        if source_dir.resolve() not in confined_source.parents:
            raise ValueError("source artifact escapes its capture directory")
        self._preflight_waveform(confined_source)
        source_sha256 = _sha256(confined_source)
        recorded_hashes = source_manifest.get("sha256")
        recorded_source_hash = (
            recorded_hashes.get(source_artifact)
            if isinstance(recorded_hashes, dict)
            else None
        )
        if recorded_source_hash != source_sha256:
            raise ValueError("source waveform hash does not match its immutable manifest")
        time_us, can_h, can_l = self._load_waveform(confined_source)
        decoded = decode_can_waveform(time_us, can_h, can_l)
        frames = sorted(
            decoded["frames"],
            key=lambda frame: (
                int(frame["identifier"]),
                bool(frame["extended"]),
                float(frame["timestamp_us"]),
            ),
        )
        if not frames:
            raise ValueError("source contains no validated Classical CAN frames")
        identifiers = aggregate_can_identifiers(frames)
        if _sha256(confined_source) != source_sha256:
            raise RuntimeError("source waveform changed during decode")

        captured_at = datetime.now(UTC)
        run_id = (
            captured_at.strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{_slugify(label)}-can-decode"
        )
        partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=self.data_dir))
        final = self.data_dir / run_id
        capture_id: int | None = None
        source_record = (
            self.database.get_capture_by_run_id(request.source_run_id)
            if self.database is not None
            else None
        )
        source_capture_id = source_record.get("id") if source_record else None
        settings = {
            "decoder_version": DECODER_VERSION,
            "classical_can_only": True,
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_source_samples": MAX_SOURCE_SAMPLES,
            "max_sample_interval_us": MAX_SAMPLE_INTERVAL_US,
        }
        common: dict[str, Any] = {
            "run_id": run_id,
            "captured_at": captured_at.isoformat(),
            "label": label,
            "capture_type": "can_decode",
            "source_run_id": request.source_run_id,
            "parent_run_id": request.source_run_id,
            "source_capture_id": source_capture_id if isinstance(source_capture_id, int) else None,
            "parent_capture_id": source_capture_id if isinstance(source_capture_id, int) else None,
            "source_artifact": source_artifact,
            "source_sha256": source_sha256,
            "decoder": settings,
            "can_polarity": decoded["polarity"],
            "nominal_bitrate_bps": decoded["nominal_bitrate_bps"],
            "frame_count": len(frames),
            "identifier_count": len(identifiers),
            "rejected_candidate_count": int(decoded["rejected_candidate_count"]),
            "unsupported_fd_candidate_count": int(
                decoded.get("unsupported_fd_candidate_count", 0)
            ),
            "writes_performed": 0,
            "warnings": list(decoded["warnings"]),
            "limitations": LIMITATIONS,
        }
        try:
            if self.database is not None:
                capture_id = self.database.create_capture(
                    session_id=source_record.get("session_id") if source_record else None,
                    run_id=run_id,
                    captured_at=captured_at.isoformat(),
                    capture_type="can_decode",
                    label=label,
                    backend=f"can-decoder-v{DECODER_VERSION}",
                    preset=f"{decoded['nominal_bitrate_bps']}bps",
                    samples=len(frames),
                    sample_interval_us=float(np.median(np.diff(time_us))),
                    duration_ms=float((time_us[-1] - time_us[0]) / 1000.0),
                    metadata=dict(common),
                )
                common["capture_id"] = capture_id
                self.database.set_capture_metadata(capture_id, dict(common))
            self._write_frames(partial / "frames.jsonl", frames)
            self._write_identifiers(partial / "identifiers.csv", identifiers)
            summary = dict(common)
            summary["identifiers"] = identifiers
            (partial / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            artifacts = ["frames.jsonl", "identifiers.csv", "summary.json", "manifest.json"]
            artifact_hashes = {
                name: _sha256(partial / name)
                for name in artifacts
                if name != "manifest.json"
            }
            manifest = dict(common)
            manifest.update({"artifacts": artifacts, "sha256": artifact_hashes, "summary": summary})
            (partial / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            partial.rename(final)
            if self.database is not None and capture_id is not None:
                media = {
                    "frames.jsonl": ("can_frames", "application/x-ndjson"),
                    "identifiers.csv": ("can_identifiers", "text/csv"),
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
    def _preflight_waveform(path: Path) -> None:
        size = path.stat().st_size
        if size <= 0 or size > MAX_SOURCE_BYTES:
            raise ValueError("source waveform exceeds the bounded file-size limit")
        row_count = 0
        with path.open("rb") as handle:
            for row_count, _line in enumerate(handle, start=1):
                if row_count > MAX_SOURCE_SAMPLES + 1:
                    raise ValueError("source waveform exceeds the bounded sample-count limit")
        if row_count < 4:
            raise ValueError("source waveform is malformed or has too few samples")

    @staticmethod
    def _load_waveform(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        CanDecodeManager._preflight_waveform(path)
        with path.open("r", encoding="utf-8", newline="") as handle:
            header_line = handle.readline(4097)
            if not header_line.endswith("\n") or len(header_line) > 4096:
                raise ValueError("source waveform header is missing or too long")
        headers = next(csv.reader([header_line]))
        if len(headers) != len(set(headers)):
            raise ValueError("source waveform header contains duplicate columns")
        schema_indices = {
            ("time_us", "vbat_v", "can_h_v", "can_l_v"): (0, 2, 3),
            (
                "time_us", "vbat_v", "can_h_v", "can_l_v",
                "diff_b_minus_c_v", "common_mode_v",
            ): (0, 2, 3),
            ("time_us", "VBAT", "CAN-H", "CAN-L"): (0, 2, 3),
            ("time_us", "B", "C"): (0, 1, 2),
        }
        indices = schema_indices.get(tuple(headers))
        if indices is None:
            raise ValueError("source waveform is malformed: header does not match a supported exact schema")
        try:
            values = np.loadtxt(
                path,
                delimiter=",",
                skiprows=1,
                usecols=tuple(indices),
                dtype=np.float64,
                ndmin=2,
                max_rows=MAX_SOURCE_SAMPLES + 1,
            )
        except (OSError, ValueError) as exc:
            raise ValueError("source waveform is malformed") from exc
        if values.shape[0] < 3:
            raise ValueError("source waveform has too few samples")
        if values.shape[0] > MAX_SOURCE_SAMPLES:
            raise ValueError("source waveform exceeds the bounded sample-count limit")
        time_us, can_h, can_l = values.T
        if not np.all(np.isfinite(values)) or np.any(np.diff(time_us) <= 0):
            raise ValueError("source waveform samples must be finite with increasing timestamps")
        sample_interval_us = float(np.median(np.diff(time_us)))
        intervals = np.diff(time_us)
        if np.max(np.abs(intervals - sample_interval_us)) > max(
            1e-9, sample_interval_us * 0.02
        ):
            raise ValueError("source waveform timebase is materially irregular")
        if sample_interval_us > MAX_SAMPLE_INTERVAL_US:
            raise ValueError("source waveform sampling resolution is unsupported")
        return time_us, can_h, can_l

    @staticmethod
    def _write_frames(path: Path, frames: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for frame in frames:
                handle.write(json.dumps(frame, sort_keys=True, separators=(",", ":")) + "\n")

    @staticmethod
    def _write_identifiers(path: Path, identifiers: list[dict[str, Any]]) -> None:
        columns = [
            "identifier", "identifier_hex", "format", "frame_count",
            "first_timestamp_us", "last_timestamp_us", "observed_duration_us",
            "mean_period_us", "mean_frequency_hz", "min_interval_us",
            "max_interval_us", "payload_change_count", "last_payload_hex",
            "byte_change_counts",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for item in identifiers:
                row = {
                    "identifier": item["identifier"],
                    "identifier_hex": item["identifier_hex"],
                    "format": "extended" if item["extended"] else "standard",
                    "frame_count": item["frame_count"],
                    "first_timestamp_us": item["first_timestamp_us"],
                    "last_timestamp_us": item["last_timestamp_us"],
                    "observed_duration_us": item["observed_duration_us"],
                    "mean_period_us": item["mean_period_us"],
                    "mean_frequency_hz": item["mean_frequency_hz"],
                    "min_interval_us": item["min_interval_us"],
                    "max_interval_us": item["max_interval_us"],
                    "payload_change_count": item["payload_change_count"],
                    "last_payload_hex": item["last_payload_hex"],
                    "byte_change_counts": ";".join(str(value) for value in item["byte_change_counts"]),
                }
                writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
