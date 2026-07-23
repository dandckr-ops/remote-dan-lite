from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Literal
from uuid import uuid4

from remote_dan.database import EvidenceDatabase
from remote_dan.obd_service import OBDService


SnapshotKind = Literal["live", "faults", "vehicle_info"]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "obd-snapshot"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def save(self, *, kind: SnapshotKind, label: str) -> dict[str, Any]:
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
        captured_at = str(
            data.get("observed_at") or data.get("sampled_at") or _utc_now()
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        run_id = f"{timestamp}-{_slugify(label)}-{uuid4().hex[:8]}"
        partial = self.data_dir / f".{run_id}.partial"
        final = self.data_dir / run_id
        partial.mkdir(parents=True, exist_ok=False)
        capture_id: int | None = None
        try:
            snapshot_document = {
                "schema_version": 1,
                "kind": kind,
                "label": label,
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
                else sum(
                    len(data.get(group, []))
                    for group in ("stored", "pending", "permanent")
                )
                if kind == "faults"
                else len(data.get("vins", []))
            )
            capture_id = self.database.create_capture(
                session_id=int(session_id),
                run_id=run_id,
                captured_at=captured_at,
                capture_type="obd_scan",
                label=label,
                preset=kind,
                backend=str(status["provider"]),
                samples=sample_count,
                sample_interval_us=None,
                duration_ms=None,
                status="pending",
                metadata={
                    "profile": "obd",
                    "channels": list(status["responder_ids"]),
                },
            )
            raw_responses = dict(data.get("raw_responses", {}))
            parsed = {key: value for key, value in data.items() if key != "raw_responses"}
            snapshot_id = self.database.create_obd_snapshot(
                connection_id=int(status["connection_id"]),
                session_id=int(session_id),
                capture_id=capture_id,
                kind=kind,
                provider=str(status["provider"]),
                protocol=str(status["protocol"]),
                raw_responses=raw_responses,
                parsed=parsed,
            )
            if kind == "faults":
                self.database.add_obd_dtcs(
                    snapshot_id,
                    [
                        item
                        for group in ("stored", "pending", "permanent")
                        for item in data.get(group, [])
                    ],
                )
            elif kind == "live":
                self.database.add_obd_live_values(
                    snapshot_id,
                    list(data.get("values", [])),
                )
            artifacts = ["obd-snapshot.json", "manifest.json"]
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "capture_id": capture_id,
                "obd_snapshot_id": snapshot_id,
                "captured_at": captured_at,
                "capture_type": "obd_scan",
                "label": label,
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
                    "errors": data.get("errors", []),
                },
            }
            manifest_path = partial / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            partial.rename(final)
            metadata = {
                "obd-snapshot.json": ("obd_snapshot", "application/json"),
                "manifest.json": ("manifest", "application/json"),
            }
            for name in artifacts:
                path = final / name
                artifact_kind, media_type = metadata[name]
                self.database.add_artifact(
                    capture_id=capture_id,
                    kind=artifact_kind,
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
            if capture_id is not None:
                try:
                    self.database.set_capture_status(capture_id, "failed")
                except Exception:
                    pass
            raise
