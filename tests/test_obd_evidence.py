from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from remote_dan.app import create_app
from remote_dan.database import EvidenceDatabase
from remote_dan.obd_evidence import OBDEvidenceManager
from remote_dan.obd_provider import SimulatorOBDProvider
from remote_dan.obd_service import OBDService


class ArtifactFailureDatabase(EvidenceDatabase):
    def create_obd_evidence(self, **kwargs: Any) -> tuple[int, int]:
        publisher = kwargs["publish_artifacts"]

        def duplicate_publisher(capture_id: int, snapshot_id: int) -> list[dict[str, Any]]:
            records = publisher(capture_id, snapshot_id)
            return [*records, dict(records[0])]

        kwargs["publish_artifacts"] = duplicate_publisher
        return super().create_obd_evidence(**kwargs)


class PartialLiveProvider(SimulatorOBDProvider):
    def query(self, command: str) -> str:
        canonical = command.replace(" ", "").upper()
        valid = super().query(canonical)
        if canonical == "010C":
            return "7E9 03 7F 01 12 00 00 00\r" + valid
        return valid


class ReadinessOnlyPartialProvider(SimulatorOBDProvider):
    def query(self, command: str) -> str:
        canonical = command.replace(" ", "").upper()
        if canonical in {"03", "07", "0A"}:
            return "7E9 24 01 02 03 04 05 06 07\r>"
        return super().query(canonical)


def _connected_service(database: EvidenceDatabase) -> tuple[OBDService, int, int]:
    customer_id = database.create_customer(name="Bench")
    vehicle_id = database.create_vehicle(display_name="Synthetic vehicle")
    context = database.create_diagnostic_session(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        title="OBD evidence",
        purpose="Regression test",
    )
    service = OBDService(database=database, simulator_provider=SimulatorOBDProvider())
    status = service.connect(mode="simulator", session_id=context["session_id"])
    return service, int(context["session_id"]), int(status["connection_id"])


class ChangingConnectionService:
    def __init__(self) -> None:
        self.changed = False

    def status(self) -> dict[str, Any]:
        return {
            "connected": True,
            "session_id": 1 if not self.changed else 2,
            "connection_id": 10 if not self.changed else 20,
            "connection_generation": "first" if not self.changed else "second",
            "provider": "obd-simulator",
            "adapter_identity": "fixture",
            "protocol": "ISO 15765-4",
            "responder_ids": ["7E8"],
            "voltage": 13.8,
        }

    def read_faults(self) -> dict[str, Any]:
        self.changed = True
        return {
            "observed_at": "2026-07-23T00:00:00+00:00",
            "readiness": [],
            "stored": [],
            "pending": [],
            "permanent": [],
            "errors": [],
            "raw_responses": {},
        }

    def read_live(self) -> dict[str, Any]:
        raise AssertionError("unexpected live read")

    def read_vehicle_info(self) -> dict[str, Any]:
        raise AssertionError("unexpected vehicle-info read")


def test_obd_evidence_rejects_connection_generation_change(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    captures = tmp_path / "captures"
    manager = OBDEvidenceManager(
        captures,
        database=database,
        service=ChangingConnectionService(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="connection changed"):
        manager.save(
            kind="faults",
            label="race",
            operation_id="00000000-0000-4000-8000-000000000001",
        )

    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
    assert list(captures.iterdir()) == []


def test_obd_evidence_transaction_rolls_back_all_rows_on_artifact_failure(
    tmp_path: Path,
) -> None:
    database = ArtifactFailureDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    service, _, _ = _connected_service(database)
    captures = tmp_path / "captures"
    manager = OBDEvidenceManager(captures, database=database, service=service)

    with pytest.raises(sqlite3.IntegrityError):
        manager.save(
            kind="faults",
            label="forced failure",
            operation_id="00000000-0000-4000-8000-000000000002",
        )

    with sqlite3.connect(database.path) as connection:
        for table in ("captures", "obd_snapshots", "obd_dtc_records", "artifacts"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert list(captures.iterdir()) == []


def test_obd_evidence_repository_rejects_mismatched_connection_session(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    _, _, connection_id = _connected_service(database)
    second_customer = database.create_customer(name="Other")
    second_vehicle = database.create_vehicle(display_name="Other vehicle")
    second = database.create_diagnostic_session(
        customer_id=second_customer,
        vehicle_id=second_vehicle,
        title="Other session",
        purpose="Mismatch proof",
    )
    published = False

    def publisher(_capture_id: int, _snapshot_id: int) -> list[dict[str, Any]]:
        nonlocal published
        published = True
        return []

    with pytest.raises(ValueError, match="do not match"):
        database.create_obd_evidence(
            connection_id=connection_id,
            session_id=int(second["session_id"]),
            run_id="mismatch-run",
            captured_at="2026-07-23T00:00:00+00:00",
            kind="faults",
            label="Mismatch",
            provider="obd-simulator",
            protocol="ISO 15765-4 CAN 11/500 (simulated)",
            responder_ids=["7E8"],
            sample_count=0,
            raw_responses={},
            parsed={},
            dtcs=[],
            live_values=[],
            publish_artifacts=publisher,
        )

    assert published is False
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM obd_snapshots").fetchone()[0] == 0


def test_obd_evidence_manager_quarantines_orphaned_published_run(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    captures = tmp_path / "captures"
    orphan = captures / "orphan-obd-run"
    orphan.mkdir(parents=True)
    (orphan / "manifest.json").write_text(
        json.dumps({"run_id": orphan.name, "capture_type": "obd_scan"}),
        encoding="utf-8",
    )

    manager = OBDEvidenceManager(
        captures,
        database=database,
        service=ChangingConnectionService(),  # type: ignore[arg-type]
    )

    assert not orphan.exists()
    assert (manager.work_dir / "quarantine" / orphan.name / "manifest.json").is_file()


def test_obd_evidence_marks_valid_mixed_responder_snapshot_partial(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    customer_id = database.create_customer(name="Bench")
    vehicle_id = database.create_vehicle(display_name="Synthetic vehicle")
    context = database.create_diagnostic_session(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        title="Partial evidence",
        purpose="Regression test",
    )
    service = OBDService(database=database, simulator_provider=PartialLiveProvider())
    service.connect(mode="simulator", session_id=int(context["session_id"]))
    manager = OBDEvidenceManager(tmp_path / "captures", database=database, service=service)

    manifest = manager.save(
        kind="live",
        label="partial live",
        operation_id="00000000-0000-4000-8000-000000000077",
    )

    with sqlite3.connect(database.path) as connection:
        capture_status = connection.execute(
            "SELECT status FROM captures WHERE id = ?", (manifest["capture_id"],)
        ).fetchone()[0]
        snapshot_status = connection.execute(
            "SELECT status FROM obd_snapshots WHERE id = ?", (manifest["obd_snapshot_id"],)
        ).fetchone()[0]
    assert capture_status == "complete"
    assert snapshot_status == "partial"
    assert manifest["summary"]["status"] == "partial"

    client = TestClient(create_app(data_dir=tmp_path / "captures", db_path=database.path))
    assert any(item["run_id"] == manifest["run_id"] for item in client.get("/api/captures").json())
    assert client.get(f"/artifacts/{manifest['run_id']}/obd-snapshot.json").status_code == 200


def test_obd_evidence_idempotency_key_is_bound_to_request_and_session(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    service, _, _ = _connected_service(database)
    manager = OBDEvidenceManager(tmp_path / "captures", database=database, service=service)
    operation_id = "00000000-0000-4000-8000-000000000088"
    original = manager.save(kind="live", label="Original", operation_id=operation_id)

    assert manager.save(kind="live", label="Original", operation_id=operation_id)["run_id"] == original["run_id"]
    with pytest.raises(ValueError, match="does not match the original evidence request"):
        manager.save(kind="faults", label="Original", operation_id=operation_id)
    with pytest.raises(ValueError, match="does not match the original evidence request"):
        manager.save(kind="live", label="Changed", operation_id=operation_id)

    service.disconnect()
    customer_id = database.create_customer(name="Other bench")
    vehicle_id = database.create_vehicle(display_name="Other vehicle")
    second = database.create_diagnostic_session(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        title="Other session",
        purpose="Replay isolation",
    )
    service.connect(mode="simulator", session_id=int(second["session_id"]))
    with pytest.raises(ValueError, match="different diagnostic session"):
        manager.save(kind="live", label="Original", operation_id=operation_id)


def test_fault_evidence_preserves_valid_readiness_when_all_dtc_services_fail(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    customer_id = database.create_customer(name="Bench")
    vehicle_id = database.create_vehicle(display_name="Vehicle")
    context = database.create_diagnostic_session(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        title="Readiness partial",
        purpose="Regression",
    )
    service = OBDService(database=database, simulator_provider=ReadinessOnlyPartialProvider())
    service.connect(mode="simulator", session_id=int(context["session_id"]))
    manager = OBDEvidenceManager(tmp_path / "captures", database=database, service=service)

    manifest = manager.save(
        kind="faults",
        label="Readiness survives",
        operation_id="00000000-0000-4000-8000-000000000089",
    )
    snapshot = database.get_obd_snapshot(int(manifest["obd_snapshot_id"]))

    assert snapshot is not None
    assert snapshot["status"] == "partial"
    assert len(snapshot["parsed"]["readiness"]) == 1
    assert manifest["samples"] == 1
