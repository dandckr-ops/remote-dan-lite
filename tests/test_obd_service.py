from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from remote_dan.database import EvidenceDatabase
from remote_dan.obd_provider import OBDNotConnected, SimulatorOBDProvider
from remote_dan.obd_service import OBDService


class FailingDisconnectProvider(SimulatorOBDProvider):
    def disconnect(self) -> None:
        super().disconnect()
        raise OSError("adapter unplugged")


class MixedResponderProvider(SimulatorOBDProvider):
    def query(self, command: str) -> str:
        canonical = command.replace(" ", "").upper()
        valid = super().query(canonical)
        if canonical == "010C":
            return "7E9 03 7F 01 12 00 00 00\r" + valid
        if canonical == "03":
            return "7E9 03 7F 03 12 00 00 00\r" + valid
        if canonical == "0902":
            return "7E9 05 49 02 01 42 41 44\r" + valid
        return valid


class MalformedIsoTpPeerProvider(SimulatorOBDProvider):
    def query(self, command: str) -> str:
        canonical = command.replace(" ", "").upper()
        valid = super().query(canonical)
        if canonical == "010C":
            return valid.replace(">", "") + "7E9 24 01 02 03 04 05 06 07\r>"
        return valid


class WrongPidResponderProvider(SimulatorOBDProvider):
    def query(self, command: str) -> str:
        canonical = command.replace(" ", "").upper()
        if canonical == "010C":
            return "7E8 03 41 0D 37 00 00 00\r>"
        return super().query(canonical)


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
        {"ecu": "7E8", "vin": "RDLTEST1234567890"},
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


def test_obd_service_records_disconnect_failure_and_clears_owned_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "remote-dan.sqlite3"
    database = EvidenceDatabase(db_path)
    database.initialize()
    service = OBDService(
        database=database,
        simulator_provider=FailingDisconnectProvider(),
    )
    connected = service.connect(mode="simulator", session_id=None)

    with pytest.raises(OSError, match="unplugged"):
        service.disconnect()

    assert service.status()["connected"] is False
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, error, ended_at FROM obd_connections WHERE id = ?",
            (connected["connection_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "error"
    assert "unplugged" in row[1]
    assert row[2] is not None


def test_obd_service_preserves_valid_ecu_results_when_another_ecu_is_invalid(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(database=database, simulator_provider=MixedResponderProvider())
    service.connect(mode="simulator", session_id=None)

    live = service.read_live()
    faults = service.read_faults()
    vehicle = service.read_vehicle_info()

    assert any(item["ecu"] == "7E8" and item["pid"] == "0C" for item in live["values"])
    assert any(item.get("ecu") == "7E9" for item in live["errors"])
    assert [item["code"] for item in faults["stored"]] == ["P0102", "P0113", "P0028"]
    assert faults["stored_status"] == "partial"
    assert any(item.get("ecu") == "7E9" for item in faults["errors"])
    assert vehicle["vins"] == [{"ecu": "7E8", "vin": "RDLTEST1234567890"}]
    assert any(item.get("ecu") == "7E9" for item in vehicle["errors"])


def test_obd_service_preserves_valid_ecu_when_peer_iso_tp_is_malformed(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(database=database, simulator_provider=MalformedIsoTpPeerProvider())
    service.connect(mode="simulator", session_id=None)

    live = service.read_live()

    assert any(item["ecu"] == "7E8" and item["pid"] == "0C" for item in live["values"])
    assert any(
        item.get("ecu") == "7E9" and "continuation without first frame" in item["error"]
        for item in live["errors"]
    )


def test_obd_service_clears_owned_state_when_database_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = EvidenceDatabase(tmp_path / "remote-dan.sqlite3")
    database.initialize()
    service = OBDService(database=database, simulator_provider=SimulatorOBDProvider())
    service.connect(mode="simulator", session_id=None)

    def fail_close(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "close_obd_connection", fail_close)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        service.disconnect()

    assert service.status()["connected"] is False


def test_live_read_rejects_response_for_a_different_requested_pid(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "obd.sqlite3")
    database.initialize()
    service = OBDService(database=database, simulator_provider=WrongPidResponderProvider())
    service.connect(mode="simulator", session_id=None)

    payload = service.read_live()

    assert not any(item["pid"] == "0C" for item in payload["values"])
    assert sum(item["pid"] == "0D" for item in payload["values"]) == 1
    assert any(
        item["command"] == "010C" and "does not match requested PID 0C" in item["error"]
        for item in payload["errors"]
    )


def test_connect_preserves_primary_error_and_reports_cleanup_failure(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "obd.sqlite3")
    database.initialize()
    provider = FailingDisconnectProvider()
    service = OBDService(database=database, simulator_provider=provider)

    with pytest.raises(ValueError, match="diagnostic session does not exist") as caught:
        service.connect(mode="simulator", session_id=999)

    assert provider.connected is False
    assert service.status()["connected"] is False
    assert any(
        "cleanup" in note.lower() and "adapter unplugged" in note
        for note in getattr(caught.value, "__notes__", [])
    )
