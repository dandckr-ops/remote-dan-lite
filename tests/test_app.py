from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from remote_dan.app import create_app
from remote_dan.capture import SimulatorBackend


class FakePicoBackend(SimulatorBackend):
    name = "ps2000a"


def test_status_reports_degraded_hardware_and_available_simulator(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path, hardware_probe=lambda: {
        "driver_available": False,
        "device_present": False,
        "reason": "Pico ARM64 driver unavailable",
    })

    response = TestClient(app).get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "remote-dan-lite"
    assert payload["capture_ready"] is True
    assert payload["default_backend"] == "simulator"
    assert payload["hardware"]["driver_available"] is False


def test_index_is_traceworks_capture_console(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Traceworks" in response.text
    assert "Scope setup" in response.text
    assert "Commissioned network capture" in response.text
    assert "Field Journal" in response.text


def test_api_can_trigger_and_list_a_simulated_capture(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    created = client.post("/api/captures", json={
        "label": "bench CAN",
        "preset": "short",
        "mode": "simulator",
    })

    assert created.status_code == 201
    manifest = created.json()
    assert manifest["backend"] == "simulator"

    listed = client.get("/api/captures")
    assert listed.status_code == 200
    assert listed.json()[0]["run_id"] == manifest["run_id"]

    artifact = client.get(f"/artifacts/{manifest['run_id']}/summary.json")
    assert artifact.status_code == 200
    assert artifact.json()["backend"] == "simulator"


def test_api_persists_capture_and_artifact_lineage_in_sqlite(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "remote-dan.sqlite3",
    )
    client = TestClient(app)

    created = client.post("/api/captures", json={
        "label": "database proof",
        "preset": "short",
        "mode": "simulator",
        "capture_type": "scope",
    })

    assert created.status_code == 201
    capture_id = created.json()["capture_id"]

    evidence = client.get(f"/api/evidence/captures/{capture_id}")

    assert evidence.status_code == 200
    assert evidence.json()["id"] == capture_id
    assert evidence.json()["run_id"] == created.json()["run_id"]
    assert len(evidence.json()["artifacts"]) == 5


def test_hardware_mode_fails_closed_when_driver_is_unavailable(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path, hardware_probe=lambda: {
        "driver_available": False,
        "device_present": False,
        "reason": "unsupported ARM64 driver",
    })

    response = TestClient(app).post("/api/captures", json={
        "label": "real scope",
        "preset": "short",
        "mode": "hardware",
    })

    assert response.status_code == 503
    assert "unsupported ARM64 driver" in response.json()["detail"]


def test_hardware_mode_uses_the_wired_pico_backend_when_ready(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path,
        hardware_probe=lambda: {
            "driver_available": True,
            "device_present": True,
            "reason": "ready",
        },
        hardware_backend=FakePicoBackend(seed=2406),
    )

    response = TestClient(app).post("/api/captures", json={
        "label": "real scope",
        "preset": "short",
        "mode": "hardware",
    })

    assert response.status_code == 201
    assert response.json()["backend"] == "ps2000a"


def test_serial_status_and_simulator_capture_are_independent_of_pico(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
        hardware_probe=lambda: {
            "driver_available": False,
            "device_present": False,
            "reason": "Pico unavailable",
        },
        serial_probe=lambda: {
            "device_present": True,
            "model": "SEL C662 Serial Cable",
            "stable_path": "/dev/serial/by-id/sel-test",
            "device_path": "/dev/ttyUSB0",
            "reason": "SEL C662 ready for receive-only capture",
        },
    )
    client = TestClient(app)

    status = client.get("/api/status")
    created = client.post("/api/serial/captures", json={
        "label": "SEL receive proof",
        "duration_s": 2,
        "mode": "simulator",
        "baud": 9600,
        "data_bits": 8,
        "parity": "N",
        "stop_bits": 1,
    })

    assert status.status_code == 200
    assert status.json()["serial_hardware"]["device_present"] is True
    assert created.status_code == 201
    manifest = created.json()
    assert manifest["capture_type"] == "serial"
    assert manifest["backend"] == "serial-simulator"
    assert manifest["summary"]["serial_analysis"]["protocol"]["name"] == "SEL ASCII / terminal"
    evidence = client.get(f"/api/evidence/captures/{manifest['capture_id']}")
    assert evidence.status_code == 200
    assert len(evidence.json()["artifacts"]) == 7


def test_serial_hardware_capture_fails_closed_when_c662_is_absent(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path,
        serial_probe=lambda: {
            "device_present": False,
            "stable_path": None,
            "reason": "SEL C662 serial cable not detected",
        },
    )

    response = TestClient(app).post("/api/serial/captures", json={
        "label": "real serial",
        "duration_s": 1,
        "mode": "hardware",
        "baud": 9600,
        "data_bits": 8,
        "parity": "N",
        "stop_bits": 1,
    })

    assert response.status_code == 503
    assert "not detected" in response.json()["detail"]
