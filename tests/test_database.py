from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from remote_dan.capture import CaptureManager, CaptureRequest, SimulatorBackend
from remote_dan.database import EvidenceDatabase, SCHEMA_SQL


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


def test_complete_capture_with_artifacts_is_atomic(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    capture_id = database.create_capture(
        run_id="atomic-child",
        captured_at="2026-07-23T12:00:00+00:00",
        capture_type="can_decode",
        label="atomic",
        backend="test",
    )
    artifact = {
        "kind": "summary",
        "filename": "summary.json",
        "relative_path": "atomic-child/summary.json",
        "media_type": "application/json",
        "size_bytes": 2,
        "sha256": "0" * 64,
    }

    with pytest.raises(sqlite3.IntegrityError):
        database.complete_capture_with_artifacts(
            capture_id,
            [artifact, dict(artifact)],
        )

    saved = database.get_capture(capture_id)
    assert saved is not None
    assert saved["status"] == "pending"
    assert saved["artifacts"] == []


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


def test_database_migrates_v1_without_losing_existing_evidence(tmp_path: Path) -> None:
    path = tmp_path / "remote-dan.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        """
        INSERT INTO captures (
            run_id, captured_at, capture_type, label, backend, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("legacy-run", "2026-07-23T12:00:00+00:00", "scope", "Legacy", "simulator", "complete"),
    )
    capture_id = int(connection.execute("SELECT id FROM captures").fetchone()[0])
    connection.execute(
        """
        INSERT INTO artifacts (
            capture_id, created_at, kind, filename, relative_path,
            media_type, size_bytes, sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            capture_id,
            "2026-07-23T12:00:01+00:00",
            "manifest",
            "manifest.json",
            "legacy-run/manifest.json",
            "application/json",
            42,
            "a" * 64,
        ),
    )
    connection.commit()
    connection.close()

    database = EvidenceDatabase(path)
    database.initialize()

    migrated = sqlite3.connect(path)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2
    assert migrated.execute("SELECT run_id FROM captures").fetchone()[0] == "legacy-run"
    assert migrated.execute("SELECT filename FROM artifacts").fetchone()[0] == "manifest.json"
    assert "customer_id" in {
        row[1] for row in migrated.execute("PRAGMA table_info(diagnostic_cases)")
    }
    assert migrated.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'customers'"
    ).fetchone() is not None
    migrated.close()


def test_database_rejects_duplicate_legacy_vins_without_partial_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "remote-dan.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute("PRAGMA user_version = 1")
        connection.executemany(
            """
            INSERT INTO assets (created_at, asset_type, display_name, vin_serial)
            VALUES ('2026-07-23T00:00:00+00:00', 'vehicle', ?, ?)
            """,
            (("Vehicle A", "DUPLICATE123"), ("Vehicle B", " duplicate123 ")),
        )

    with pytest.raises(RuntimeError, match="duplicate legacy vehicle VIN"):
        EvidenceDatabase(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(diagnostic_cases)")
        }
        assert "customer_id" not in columns
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customers'"
        ).fetchone() is None


def test_database_rejects_ambiguous_version_zero_with_existing_user_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "remote-dan.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_unknown (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="schema version 0"):
        EvidenceDatabase(path).initialize()


def test_database_initialize_is_idempotent_at_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "remote-dan.sqlite3"
    database = EvidenceDatabase(path)
    database.initialize()
    database.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_database_initialize_reconciles_connection_left_open_by_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "remote-dan.sqlite3"
    database = EvidenceDatabase(path)
    database.initialize()
    connection_id = database.create_obd_connection(
        session_id=None,
        provider="obd-simulator",
        adapter_identity="fixture",
        stable_path=None,
        protocol="ISO 15765-4 CAN 11/500 (simulated)",
        responder_ids=["7E8"],
        voltage=13.8,
    )

    database.initialize()

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT status, ended_at, error FROM obd_connections WHERE id = ?",
            (connection_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "error"
    assert row[1] is not None
    assert "service restart" in row[2]


def test_database_creates_customer_vehicle_and_diagnostic_session_context(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()

    customer_id = database.create_customer(
        name="Example Customer",
        company="Example Fleet",
        phone="555-0100",
        email="service@example.invalid",
        notes="Prefers text updates",
    )
    vehicle_id = database.create_vehicle(
        display_name="2009 Subaru Forester",
        vin="RDLTEST0000000001",
        make="Subaru",
        model="Forester",
        year=2009,
        engine="2.5L",
        asset_tag="customer-forester",
    )
    context = database.create_diagnostic_session(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        title="Generic OBD scan",
        purpose="Read emissions DTCs and live data",
        complaint="MIL history",
        operator_name="Daniel",
    )

    assert context["customer_id"] == customer_id
    assert context["vehicle_id"] == vehicle_id
    assert isinstance(context["case_id"], int)
    assert isinstance(context["session_id"], int)
    assert database.list_customers()[0]["name"] == "Example Customer"
    assert database.list_vehicles()[0]["vin"] == "RDLTEST0000000001"

    with pytest.raises(ValueError, match="VIN already exists"):
        database.create_vehicle(
            display_name="Duplicate",
            vin="rdltest0000000001",
        )


def test_database_persists_obd_snapshot_dtc_and_live_value_lineage(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    customer_id = database.create_customer(name="Bench Customer")
    vehicle_id = database.create_vehicle(
        display_name="Forester",
        vin="RDLTEST0000000002",
    )
    context = database.create_diagnostic_session(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        title="OBD proof",
        purpose="Read-only scan",
    )
    connection_id = database.create_obd_connection(
        session_id=context["session_id"],
        provider="obdlink-sx",
        adapter_identity="OBDLink SX r4.2 / STN1130 v4.0.1",
        stable_path="/dev/serial/by-id/obdlink-test",
        protocol="ISO 15765-4 CAN 11/500",
        responder_ids=["7E8"],
        voltage=13.9,
    )
    capture_id = database.create_capture(
        session_id=context["session_id"],
        run_id="obd-proof-001",
        captured_at="2026-07-23T15:46:02+00:00",
        capture_type="obd_scan",
        test_type="faults",
        label="Generic OBD fault snapshot",
        backend="obdlink-sx",
        status="complete",
    )
    snapshot_id = database.create_obd_snapshot(
        connection_id=connection_id,
        session_id=context["session_id"],
        capture_id=capture_id,
        kind="faults",
        provider="obdlink-sx",
        protocol="ISO 15765-4 CAN 11/500",
        raw_responses={"03": ["7E8 06 43 03 01 02 01 13"]},
        parsed={"mil_on": False, "dtc_count": 3},
    )
    database.add_obd_dtcs(
        snapshot_id,
        [
            {
                "state": "stored",
                "ecu": "7E8",
                "code": "P0102",
                "description": "Mass or volume air flow circuit low",
            },
            {
                "state": "stored",
                "ecu": "7E8",
                "code": "P0113",
                "description": "Intake air temperature circuit high",
            },
        ],
    )
    database.add_obd_live_values(
        snapshot_id,
        [
            {
                "pid": "0C",
                "name": "Engine speed",
                "value": 0.0,
                "unit": "rpm",
                "ecu": "7E8",
                "sampled_at": "2026-07-23T15:46:02+00:00",
                "fresh": True,
                "raw_hex": "0000",
                "error": None,
            }
        ],
    )
    database.close_obd_connection(connection_id, status="closed")

    snapshot = database.get_obd_snapshot(snapshot_id)

    assert snapshot is not None
    assert snapshot["capture_id"] == capture_id
    assert snapshot["session_id"] == context["session_id"]
    assert snapshot["raw_responses"]["03"][0].startswith("7E8")
    assert [item["code"] for item in snapshot["dtcs"]] == ["P0102", "P0113"]
    assert snapshot["live_values"][0]["unit"] == "rpm"
    assert database.list_obd_snapshots(context["session_id"])[0]["id"] == snapshot_id


def test_database_obd_clear_events_are_append_only(tmp_path: Path) -> None:
    path = tmp_path / "remote-dan.sqlite3"
    database = EvidenceDatabase(path)
    database.initialize()
    customer_id = database.create_customer(name="Bench Customer")
    vehicle_id = database.create_vehicle(display_name="Vehicle")
    context = database.create_diagnostic_session(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        title="Clear audit",
        purpose="Simulator clear test",
    )
    connection_id = database.create_obd_connection(
        session_id=context["session_id"],
        provider="obd-simulator",
        adapter_identity="simulator",
        stable_path=None,
        protocol="simulated ISO 15765-4",
        responder_ids=["7E8"],
        voltage=13.8,
    )

    event_id = database.record_obd_clear_event(
        session_id=context["session_id"],
        connection_id=connection_id,
        actor="Daniel",
        confirmation_text="CLEAR 000001",
        command="04",
        before_snapshot_id=None,
        after_snapshot_id=None,
        outcome="simulated_success",
        response={"7E8": "44"},
        ambiguous=False,
    )
    event = database.get_obd_clear_event(event_id)

    assert event is not None
    assert event["outcome"] == "simulated_success"
    assert event["response"] == {"7E8": "44"}

    other_customer = database.create_customer(name="Other bench")
    other_vehicle = database.create_vehicle(display_name="Other vehicle")
    other = database.create_diagnostic_session(
        customer_id=other_customer,
        vehicle_id=other_vehicle,
        title="Other clear audit",
        purpose="Lineage isolation",
    )
    other_connection = database.create_obd_connection(
        session_id=other["session_id"],
        provider="obd-simulator",
        adapter_identity="other simulator",
        stable_path=None,
        protocol="simulated ISO 15765-4",
        responder_ids=["7E9"],
        voltage=13.7,
    )
    other_snapshot = database.create_obd_snapshot(
        connection_id=other_connection,
        session_id=other["session_id"],
        capture_id=None,
        kind="faults",
        provider="obd-simulator",
        protocol="simulated ISO 15765-4",
        raw_responses={},
        parsed={},
    )
    with pytest.raises(ValueError, match="snapshot lineage"):
        database.record_obd_clear_event(
            session_id=context["session_id"],
            connection_id=connection_id,
            actor="Daniel",
            confirmation_text="CLEAR 000002",
            command="04",
            before_snapshot_id=other_snapshot,
            after_snapshot_id=None,
            outcome="blocked",
            response={},
            ambiguous=True,
        )

    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE obd_clear_events SET outcome = 'changed' WHERE id = ?",
            (event_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM obd_clear_events WHERE id = ?", (event_id,))
    connection.close()
