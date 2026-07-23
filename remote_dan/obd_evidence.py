from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any, Literal
from uuid import UUID, uuid4

from remote_dan.database import EvidenceDatabase
from remote_dan.obd_protocol import OBDProtocolError
from remote_dan.obd_service import OBDService


SnapshotKind = Literal["live", "faults", "vehicle_info"]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()



def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class OBDEvidenceManager:
    def __init__(
        self,
        data_dir: Path,
        *,
        database: EvidenceDatabase,
        service: OBDService,
    ) -> None:
        self.data_dir = data_dir
        self.database = database
        self.service = service
        self._save_lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.data_dir.parent / ".obd-work"
        self.partial_dir = self.work_dir / "partial"
        self.partial_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_orphaned_obd_runs()

    def _quarantine_orphaned_obd_runs(self) -> None:
        quarantine = self.work_dir / "quarantine"
        for partial in self.partial_dir.iterdir():
            if partial.is_dir():
                shutil.rmtree(partial, ignore_errors=True)
            else:
                partial.unlink(missing_ok=True)
        legacy_quarantine = self.data_dir / ".orphaned-obd"
        if legacy_quarantine.is_dir():
            quarantine.mkdir(parents=True, exist_ok=True)
            for legacy in legacy_quarantine.iterdir():
                target = quarantine / legacy.name
                if target.exists():
                    target = quarantine / f"{legacy.name}-{uuid4().hex[:8]}"
                legacy.rename(target)
            legacy_quarantine.rmdir()
        for candidate in list(self.data_dir.iterdir()):
            if candidate.is_dir() and candidate.name.endswith(".partial"):
                shutil.rmtree(candidate, ignore_errors=True)
                continue
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            manifest_path = candidate / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if manifest.get("capture_type") != "obd_scan":
                continue
            run_id = str(manifest.get("run_id") or "")
            if run_id == candidate.name and self.database.capture_exists_for_run(run_id):
                continue
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / candidate.name
            if target.exists():
                target = quarantine / f"{candidate.name}-{uuid4().hex[:8]}"
            candidate.rename(target)
        if quarantine.exists():
            _fsync_directory(quarantine)
            _fsync_directory(self.data_dir)

    def save(
        self,
        *,
        kind: SnapshotKind,
        label: str,
        operation_id: str,
    ) -> dict[str, Any]:
        run_id = f"obd-{UUID(operation_id)}"
        clean_label = label.strip() or "OBD snapshot"
        with self._save_lock:
            final = self.data_dir / run_id
            manifest_path = final / "manifest.json"
            if self.database.capture_exists_for_run(run_id) and manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("preset") != kind or manifest.get("label") != clean_label:
                    raise ValueError(
                        "operation ID does not match the original evidence request"
                    )
                snapshot_id = manifest.get("obd_snapshot_id")
                snapshot = (
                    self.database.get_obd_snapshot(int(snapshot_id))
                    if snapshot_id is not None
                    else None
                )
                if snapshot is None or snapshot.get("kind") != kind:
                    raise RuntimeError("operation ID has inconsistent committed evidence")
                status = self.service.status()
                if not status.get("connected"):
                    raise ValueError("OBD is not connected for evidence replay")
                if snapshot.get("session_id") != status.get("session_id"):
                    raise ValueError("operation ID belongs to a different diagnostic session")
                if snapshot.get("connection_id") != status.get("connection_id"):
                    raise ValueError("operation ID belongs to a different OBD connection")
                return manifest
            if final.exists() or self.database.capture_exists_for_run(run_id):
                raise RuntimeError("OBD operation ID has inconsistent existing evidence")
            return self._save_locked(kind=kind, label=clean_label, run_id=run_id)

    def _save_locked(
        self,
        *,
        kind: SnapshotKind,
        label: str,
        run_id: str,
    ) -> dict[str, Any]:
        status = self.service.status()
        if not status["connected"]:
            raise ValueError("OBD is not connected")
        session_id = status.get("session_id")
        if session_id is None:
            raise ValueError("select a diagnostic session before saving OBD evidence")
        readers = {
            "live": self.service.read_live,
            "faults": self.service.read_faults,
            "vehicle_info": self.service.read_vehicle_info,
        }
        data = readers[kind]()
        observed_status = self.service.status()
        connection_keys = (
            "connection_id",
            "connection_generation",
            "session_id",
            "provider",
        )
        if (
            not observed_status.get("connected")
            or any(observed_status.get(key) != status.get(key) for key in connection_keys)
        ):
            raise RuntimeError("OBD connection changed while reading evidence snapshot")

        clean_label = label
        captured_at = str(data.get("observed_at") or data.get("sampled_at") or _utc_now())
        partial = self.partial_dir / f"{run_id}.partial"
        final = self.data_dir / run_id
        partial.mkdir(parents=True, exist_ok=False)
        published = False
        manifest_result: dict[str, Any] | None = None
        try:
            snapshot_document = {
                "schema_version": 1,
                "kind": kind,
                "label": clean_label,
                "captured_at": captured_at,
                "session_id": session_id,
                "connection": {
                    "connection_id": status["connection_id"],
                    "provider": status["provider"],
                    "adapter_identity": status["adapter_identity"],
                    "protocol": status["protocol"],
                    "responder_ids": status["responder_ids"],
                    "voltage": status["voltage"],
                    "connection_generation": status["connection_generation"],
                },
                "data": data,
            }
            snapshot_path = partial / "obd-snapshot.json"
            snapshot_path.write_text(
                json.dumps(snapshot_document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            sample_count = (
                len(data.get("values", []))
                if kind == "live"
                else len(data.get("readiness", [])) + sum(
                    len(data.get(group, []))
                    for group in ("stored", "pending", "permanent")
                )
                if kind == "faults"
                else len(data.get("vins", []))
            )
            errors = list(data.get("errors", []))
            valid_fault_observation = kind == "faults" and (
                bool(data.get("readiness"))
                or any(
                    data.get(f"{state}_status") in {"complete", "no_data", "partial"}
                    for state in ("stored", "pending", "permanent")
                )
            )
            if errors and sample_count == 0 and not valid_fault_observation:
                raise OBDProtocolError(
                    "OBD snapshot has communication/decode errors and no valid observations"
                )
            evidence_status = "partial" if errors else "complete"
            raw_responses = dict(data.get("raw_responses", {}))
            parsed = {key: value for key, value in data.items() if key != "raw_responses"}
            dtcs = (
                [
                    item
                    for group in ("stored", "pending", "permanent")
                    for item in data.get(group, [])
                ]
                if kind == "faults"
                else []
            )
            live_values = list(data.get("values", [])) if kind == "live" else []

            def publish_artifacts(capture_id: int, snapshot_id: int) -> list[dict[str, Any]]:
                nonlocal published, manifest_result
                artifacts = ["obd-snapshot.json", "manifest.json"]
                manifest_result = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "capture_id": capture_id,
                    "obd_snapshot_id": snapshot_id,
                    "captured_at": captured_at,
                    "capture_type": "obd_scan",
                    "label": clean_label,
                    "preset": kind,
                    "profile": "obd",
                    "backend": status["provider"],
                    "session_id": session_id,
                    "samples": sample_count,
                    "sample_interval_us": None,
                    "duration_ms": None,
                    "channels": status["responder_ids"],
                    "artifacts": artifacts,
                    "sha256": {"obd-snapshot.json": _sha256(snapshot_path)},
                    "summary": {
                        "kind": kind,
                        "protocol": status["protocol"],
                        "responders": status["responder_ids"],
                        "status": evidence_status,
                        "errors": errors,
                    },
                }
                manifest_path = partial / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest_result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                _fsync_file(snapshot_path)
                _fsync_file(manifest_path)
                _fsync_directory(partial)
                partial.rename(final)
                published = True
                _fsync_directory(self.partial_dir)
                _fsync_directory(self.data_dir)
                metadata = {
                    "obd-snapshot.json": ("obd_snapshot", "application/json"),
                    "manifest.json": ("manifest", "application/json"),
                }
                records: list[dict[str, Any]] = []
                for name in artifacts:
                    path = final / name
                    artifact_kind, media_type = metadata[name]
                    records.append(
                        {
                            "kind": artifact_kind,
                            "filename": name,
                            "relative_path": f"{run_id}/{name}",
                            "media_type": media_type,
                            "size_bytes": path.stat().st_size,
                            "sha256": _sha256(path),
                        }
                    )
                return records

            self.database.create_obd_evidence(
                connection_id=int(status["connection_id"]),
                session_id=int(session_id),
                run_id=run_id,
                captured_at=captured_at,
                kind=kind,
                label=clean_label,
                provider=str(status["provider"]),
                protocol=str(status["protocol"]),
                responder_ids=list(status["responder_ids"]),
                sample_count=sample_count,
                raw_responses=raw_responses,
                parsed=parsed,
                dtcs=dtcs,
                live_values=live_values,
                publish_artifacts=publish_artifacts,
                evidence_status=evidence_status,
            )
            if manifest_result is None:
                raise RuntimeError("OBD evidence publisher did not produce a manifest")
            return manifest_result
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            _fsync_directory(self.partial_dir)
            if published or final.exists():
                shutil.rmtree(final, ignore_errors=True)
                _fsync_directory(self.data_dir)
            raise
