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
    assert "Capture window" in response.text
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
