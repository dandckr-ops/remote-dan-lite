from __future__ import annotations

from pathlib import Path

import pytest

from remote_dan.database import EvidenceDatabase
from remote_dan.obd_provider import OBDNotConnected, SimulatorOBDProvider
from remote_dan.obd_service import OBDService


def test_obd_service_connects_and_discovers_supported_pids(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(
        database=database,
        simulator_provider=SimulatorOBDProvider(),
    )

    status = service.connect(mode="simulator", session_id=None)

    assert status["connected"] is True
    assert status["provider"] == "obd-simulator"
    assert status["protocol"].startswith("ISO 15765-4")
    assert status["responder_ids"] == ["7E8"]
    assert {"01", "04", "05", "0C", "0D", "11", "20", "2F", "40", "42"} <= set(
        status["supported_pids"]
    )
    assert isinstance(status["connection_id"], int)

    disconnected = service.disconnect()

    assert disconnected["connected"] is False
    with pytest.raises(OBDNotConnected):
        service.read_live()


def test_obd_service_returns_normalized_live_values(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(database=database, simulator_provider=SimulatorOBDProvider())
    service.connect(mode="simulator", session_id=None)

    live = service.read_live()

    by_pid = {item["pid"]: item for item in live["values"]}
    assert by_pid["0C"]["name"] == "Engine speed"
    assert by_pid["0C"]["value"] == 1726.0
    assert by_pid["0C"]["ecu"] == "7E8"
    assert by_pid["42"]["value"] == 14.0
    assert all(item["fresh"] is True for item in live["values"])
    assert live["errors"] == []


def test_obd_service_reads_fault_groups_and_readiness(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(database=database, simulator_provider=SimulatorOBDProvider())
    service.connect(mode="simulator", session_id=None)

    faults = service.read_faults()

    assert faults["readiness"][0]["mil_on"] is False
    assert faults["readiness"][0]["dtc_count"] == 3
    assert [item["code"] for item in faults["stored"]] == ["P0102", "P0113", "P0028"]
    assert faults["pending"] == []
    assert faults["permanent"] == []
    assert faults["permanent_status"] == "no_data"


def test_obd_service_reads_vin_without_silently_merging_ecus(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(database=database, simulator_provider=SimulatorOBDProvider())
    service.connect(mode="simulator", session_id=None)

    vehicle = service.read_vehicle_info()

    assert vehicle["vins"] == [
        {"ecu": "7E8", "vin": "1M8GDM9AXKP042788"},
    ]
    assert vehicle["vin_mismatch"] is False
    assert vehicle["protocol"].startswith("ISO 15765-4")
    assert vehicle["adapter_identity"].startswith("OBDLink SX simulator")


def test_obd_service_hardware_clear_remains_fail_closed_without_operator_auth(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(
        database=database,
        simulator_provider=SimulatorOBDProvider(allow_clear=True),
    )
    service.connect(mode="simulator", session_id=None)

    with pytest.raises(PermissionError, match="authenticated operator"):
        service.clear_faults()
