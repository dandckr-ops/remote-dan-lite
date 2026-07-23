from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from remote_dan.app import create_app
from remote_dan.capture import SimulatorBackend
from remote_dan.modbus_scan import ModbusSimulatorBackend


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


def test_usb_inventory_is_exposed_read_only_until_virtualhere_is_commissioned(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path,
        usb_inventory_probe=lambda: [{
            "key": "usb:10c4:ea60:bridge-1:1-1",
            "vendor_id": "10c4",
            "product_id": "ea60",
            "serial": "bridge-1",
            "product_name": "CP210x UART Bridge",
            "topology_path": "1-1",
            "route": "unknown",
        }],
    )

    response = TestClient(app).get("/api/usb/devices")

    assert response.status_code == 200
    assert response.json() == {
        "devices": [{
            "key": "usb:10c4:ea60:bridge-1:1-1",
            "vendor_id": "10c4",
            "product_id": "ea60",
            "serial": "bridge-1",
            "product_name": "CP210x UART Bridge",
            "topology_path": "1-1",
            "route": "unknown",
        }],
        "routing_control": {
            "available": False,
            "reason": "USB routing helper is not commissioned on this console yet.",
        },
    }


class FakeRoutingClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def request(self, payload: dict[str, object]) -> dict[str, object]:
        self.requests.append(payload)
        if payload["action"] == "status":
            return {"available": True, "inventory_revision": "a" * 64, "allowed_devices": []}
        return {"inventory_revision": payload["inventory_revision"], "allowed_devices": ["084f/c050"]}


def test_usb_routing_apply_requires_explicit_confirmation_and_uses_helper(tmp_path: Path) -> None:
    routing = FakeRoutingClient()
    app = create_app(data_dir=tmp_path, routing_client=routing)
    client = TestClient(app)
    request = {"inventory_revision": "a" * 64, "routes": {"usb:084f:c050:ecom:1-1": "virtualhere"}}

    denied = client.post("/api/usb/routing/apply", json=request)
    applied = client.post("/api/usb/routing/apply", json={**request, "confirmed": True})

    assert denied.status_code == 422
    assert applied.status_code == 200
    assert applied.json()["allowed_devices"] == ["084f/c050"]
    assert routing.requests == [{
        "action": "apply", "inventory_revision": "a" * 64, "routes": request["routes"],
    }]


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


def test_modbus_network_inventory_and_simulator_scan_are_exposed_without_write_fields(
    tmp_path: Path,
) -> None:
    networks = (
        {
            "interface": "eth0",
            "ifindex": 2,
            "address": "192.168.50.10",
            "network": "192.168.50.0/24",
        },
    )
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
        network_probe=lambda: networks,
    )
    client = TestClient(app)

    inventory = client.get("/api/modbus/networks")
    created = client.post("/api/modbus/scans", json={
        "label": "connected plant network",
        "interface": "eth0",
        "subnet": "192.168.50.0/24",
        "mode": "simulator",
        "connect_timeout_ms": 300,
        "workers": 4,
    })

    assert inventory.status_code == 200
    assert inventory.json() == {
        "networks": list(networks),
        "policy": {
            "ipv4_only": True,
            "connected_subnets_only": True,
            "max_hosts": 256,
            "max_workers": 8,
            "cooldown_seconds": 60,
            "deadline_seconds": 30,
            "writes_enabled": False,
        },
    }
    assert created.status_code == 201
    manifest = created.json()
    assert manifest["capture_type"] == "modbus_scan"
    assert manifest["backend"] == "modbus-simulator"
    assert manifest["summary"]["device_count"] == 2
    assert manifest["summary"]["writes_performed"] == 0
    assert len(client.get(
        f"/api/evidence/captures/{manifest['capture_id']}"
    ).json()["artifacts"]) == 7


def test_modbus_scan_rejects_scope_outside_connected_network(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path,
        network_probe=lambda: (
            {
                "interface": "eth0",
                "ifindex": 2,
                "address": "192.168.50.10",
                "network": "192.168.50.0/24",
            },
        ),
    )

    response = TestClient(app).post("/api/modbus/scans", json={
        "label": "unsafe scope",
        "interface": "eth0",
        "subnet": "10.0.0.0/24",
        "mode": "simulator",
    })

    assert response.status_code == 422
    assert "connected" in response.json()["detail"]


def test_modbus_inventory_excludes_virtual_interfaces(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path,
        network_probe=lambda: (
            {
                "interface": "docker0",
                "address": "172.17.4.1",
                "network": "172.17.0.0/16",
            },
        ),
    )

    response = TestClient(app).get("/api/modbus/networks")

    assert response.status_code == 200
    assert response.json()["networks"] == []


def test_modbus_network_mode_uses_injected_read_only_backend(tmp_path: Path) -> None:
    backend = ModbusSimulatorBackend()
    backend.name = "modbus-network-test"
    app = create_app(
        data_dir=tmp_path,
        network_probe=lambda: (
            {
                "interface": "eth0",
                "ifindex": 2,
                "address": "192.168.50.10",
                "network": "192.168.50.0/24",
            },
        ),
        modbus_backend=backend,
    )

    response = TestClient(app).post("/api/modbus/scans", json={
        "label": "live network contract",
        "interface": "eth0",
        "subnet": "192.168.50.0/24",
        "mode": "network",
    })

    assert response.status_code == 201
    assert response.json()["backend"] == "modbus-network-test"


def test_bus_sniffer_simulator_runs_three_window_survey_and_persists_lineage(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
    )
    client = TestClient(app)

    response = client.post("/api/bus-surveys", json={
        "label": "unknown CAN survey",
        "harness": "can-network",
        "mode": "simulator",
    })

    assert response.status_code == 201
    manifest = response.json()
    assert manifest["capture_type"] == "bus_survey"
    assert manifest["summary"]["classification"]["family"] == "CAN-family"
    assert manifest["summary"]["classification"]["workspace"] == "can"
    assert manifest["summary"]["writes_performed"] == 0
    evidence = client.get(f"/api/evidence/captures/{manifest['capture_id']}")
    assert evidence.status_code == 200
    assert len(evidence.json()["artifacts"]) == 8


def test_bus_sniffer_rejects_unverified_harness_at_schema_boundary(
    tmp_path: Path,
) -> None:
    app = create_app(data_dir=tmp_path)

    response = TestClient(app).post("/api/bus-surveys", json={
        "label": "unsafe",
        "harness": "unverified",
        "mode": "simulator",
    })

    assert response.status_code == 422


def test_bus_sniffer_hardware_mode_fails_closed_when_pico_is_unavailable(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path,
        hardware_probe=lambda: {
            "driver_available": False,
            "device_present": False,
            "reason": "Pico unavailable for survey",
        },
    )

    response = TestClient(app).post("/api/bus-surveys", json={
        "label": "real survey",
        "harness": "can-network",
        "mode": "hardware",
    })

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
