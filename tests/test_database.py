from __future__ import annotations

from pathlib import Path

import pytest

from remote_dan.capture import CaptureManager, CaptureRequest, SimulatorBackend
from remote_dan.database import EvidenceDatabase


def test_database_preserves_asset_case_session_capture_lineage(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()

    asset_id = database.create_asset(
        asset_type="vehicle",
        display_name="Test Vehicle",
        vin_serial="TESTSERIAL001",
        make="Example",
        model="Test Platform",
        year=2011,
        engine="N55",
        asset_tag="shop-car",
    )
    case_id = database.create_case(
        asset_id=asset_id,
        title="Crank, no start",
        complaint="Intermittent extended crank after hot soak",
        customer_name="Bench customer",
        location="Shop bay 1",
    )
    session_id = database.create_session(
        case_id=case_id,
        purpose="Capture crank event",
        operator_name="Field Technician",
    )
    capture_id = database.create_capture(
        session_id=session_id,
        run_id="run-001",
        captured_at="2026-07-23T12:00:00+00:00",
        capture_type="scope",
        label="Hot crank",
        backend="ps2000a",
        preset="short",
        samples=20_000,
        sample_interval_us=2,
        duration_ms=39.998,
        metadata={"trigger": "channel-a-rising"},
    )

    artifact_id = database.add_artifact(
        capture_id=capture_id,
        kind="raw_waveform",
        filename="capture.csv",
        relative_path="run-001/capture.csv",
        media_type="text/csv",
        size_bytes=1234,
        sha256="a" * 64,
    )

    record = database.get_capture(capture_id)

    assert record is not None
    assert record["id"] == capture_id
    assert record["session"]["id"] == session_id
    assert record["case"]["id"] == case_id
    assert record["asset"]["id"] == asset_id
    assert record["asset"]["vin_serial"] == "TESTSERIAL001"
    assert record["metadata"] == {"trigger": "channel-a-rising"}
    assert record["artifacts"] == [
        {
            "id": artifact_id,
            "kind": "raw_waveform",
            "filename": "capture.csv",
            "relative_path": "run-001/capture.csv",
            "media_type": "text/csv",
            "size_bytes": 1234,
            "sha256": "a" * 64,
        }
    ]


def test_database_enforces_artifact_capture_foreign_key(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()

    with pytest.raises(ValueError, match="capture does not exist"):
        database.add_artifact(
            capture_id=999,
            kind="report",
            filename="report.pdf",
            relative_path="missing/report.pdf",
            media_type="application/pdf",
            size_bytes=1,
            sha256="b" * 64,
        )


def test_capture_manager_assigns_database_ids_to_capture_and_artifacts(tmp_path: Path) -> None:
    capture_dir = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    manager = CaptureManager(
        capture_dir,
        backend=SimulatorBackend(seed=99),
        database=database,
    )

    manifest = manager.run(
        CaptureRequest(
            label="CAN wake-up",
            preset="short",
            mode="simulator",
            capture_type="scope",
        )
    )

    assert isinstance(manifest["capture_id"], int)
    saved = database.get_capture(manifest["capture_id"])
    assert saved is not None
    assert saved["run_id"] == manifest["run_id"]
    assert saved["status"] == "complete"
    assert {artifact["filename"] for artifact in saved["artifacts"]} == {
        "capture.csv",
        "manifest.json",
        "overview.png",
        "report.pdf",
        "summary.json",
    }
    assert all(artifact["size_bytes"] > 0 for artifact in saved["artifacts"])
    assert all(len(artifact["sha256"]) == 64 for artifact in saved["artifacts"])
