from __future__ import annotations

import hashlib
import copy
from pathlib import Path
import json
import shutil
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

import remote_dan.app as app_module
from remote_dan.app import create_app
from remote_dan.can_analysis import aggregate_can_identifiers
from remote_dan.capture import SimulatorBackend
from remote_dan.modbus_scan import ModbusSimulatorBackend
from remote_dan.obd_provider import SimulatorOBDProvider


class FakePicoBackend(SimulatorBackend):
    name = "ps2000a"


def _register_authoritative_can_parent(
    app: object,
    capture_root: Path,
    *,
    run_id: str,
    samples: int,
) -> dict[str, object]:
    database = app.state.database
    source_dir = capture_root / run_id
    source_dir.mkdir(parents=True, exist_ok=True)
    waveform = b"time_us,vbat_v,can_h_v,can_l_v\n0,12,3.5,1.5\n"
    waveform_sha = hashlib.sha256(waveform).hexdigest()
    (source_dir / "capture.csv").write_bytes(waveform)
    capture_id = database.create_capture(
        run_id=run_id,
        captured_at="2026-07-23T11:59:00+00:00",
        capture_type="can",
        label="authoritative source",
        backend="test",
        samples=samples,
        metadata={"profile": "network"},
    )
    manifest = {
        "run_id": run_id,
        "capture_id": capture_id,
        "capture_type": "can",
        "profile": "network",
        "sha256": {"capture.csv": waveform_sha},
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    (source_dir / "manifest.json").write_bytes(manifest_bytes)
    registrations = []
    for filename, content, media_type in (
        ("capture.csv", waveform, "text/csv"),
        ("manifest.json", manifest_bytes, "application/json"),
    ):
        registrations.append({
            "kind": "source",
            "filename": filename,
            "relative_path": f"{run_id}/{filename}",
            "media_type": media_type,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    database.complete_capture_with_artifacts(capture_id, registrations)
    return {
        "source_run_id": run_id,
        "source_capture_id": capture_id,
        "source_capture_type": "can",
        "source_profile": "network",
        "source_artifact": "capture.csv",
        "source_sha256": waveform_sha,
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_parent_samples": samples,
        "source_samples": samples,
    }


class AdapterFailingLiveProvider(SimulatorOBDProvider):
    def query(self, command: str) -> str:
        canonical = command.replace(" ", "").upper()
        if canonical.startswith("01") and canonical not in {"0100", "0120", "0140", "0160", "0180", "01A0"}:
            return "BUS ERROR\r>"
        return super().query(canonical)


class FakeHardwareOBDProvider(SimulatorOBDProvider):
    name = "obdlink-sx"


def test_status_reports_degraded_hardware_as_unavailable(tmp_path: Path) -> None:
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
    assert payload["default_backend"] == "unavailable"
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
            "route": "local",
        }],
        "routing_control": {
            "available": False,
            "reason": "USB routing helper is not commissioned on this console yet.",
        },
    }


class FakeRoutingClient:
    def __init__(self, allowed_devices: list[str] | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self.allowed_devices = allowed_devices or []

    def request(self, payload: dict[str, object]) -> dict[str, object]:
        self.requests.append(payload)
        if payload["action"] == "status":
            return {"available": True, "inventory_revision": "a" * 64, "allowed_devices": self.allowed_devices}
        return {"inventory_revision": payload["inventory_revision"], "allowed_devices": ["084f/c050"]}


def test_usb_routing_inventory_marks_existing_virtualhere_allowlist(tmp_path: Path) -> None:
    routing = FakeRoutingClient(allowed_devices=["084f/c050"])
    app = create_app(
        data_dir=tmp_path,
        routing_client=routing,
        usb_inventory_probe=lambda: [{
            "key": "usb:084f:c050:ecom:1-1",
            "vendor_id": "084f",
            "product_id": "c050",
            "serial": "ecom",
            "product_name": "ECOM",
            "topology_path": "1-1",
            "route": "unknown",
        }],
    )

    response = TestClient(app).get("/api/usb/devices")

    assert response.status_code == 200
    assert response.json()["devices"][0]["route"] == "virtualhere"


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


def test_usb_routing_apply_is_blocked_while_hardware_obd_owns_adapter(tmp_path: Path) -> None:
    routing = FakeRoutingClient()
    provider = FakeHardwareOBDProvider()
    app = create_app(
        data_dir=tmp_path,
        routing_client=routing,
        obd_hardware_provider_factory=lambda: provider,
    )
    client = TestClient(app)
    assert client.post("/api/obd/connect", json={"mode": "hardware"}).status_code == 201

    applied = client.post(
        "/api/usb/routing/apply",
        json={
            "inventory_revision": "a" * 64,
            "routes": {"usb:0403:6015:obdlink:1-1": "virtualhere"},
            "confirmed": True,
        },
    )

    assert applied.status_code == 409
    assert "OBD" in applied.json()["detail"]
    assert routing.requests == []


def test_hardware_obd_connect_waits_for_usb_routing_critical_section(tmp_path: Path) -> None:
    provider = FakeHardwareOBDProvider()
    app = create_app(
        data_dir=tmp_path,
        routing_client=FakeRoutingClient(),
        obd_hardware_provider_factory=lambda: provider,
    )
    client = TestClient(app)
    completed = threading.Event()
    result: list[int] = []
    lock = app.state.usb_routing_obd_lock
    lock.acquire()

    def connect() -> None:
        response = client.post("/api/obd/connect", json={"mode": "hardware"})
        result.append(response.status_code)
        completed.set()

    worker = threading.Thread(target=connect)
    worker.start()
    try:
        assert completed.wait(0.05) is False
        assert provider.connected is False
    finally:
        lock.release()
    worker.join(timeout=2)

    assert completed.is_set()
    assert result == [201]


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


def test_capture_detail_requires_complete_authoritative_manifest(tmp_path: Path) -> None:
    capture_root = tmp_path / "captures"
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)
    created = client.post("/api/captures", json={
        "label": "capture detail authority",
        "preset": "short",
        "mode": "simulator",
    })
    assert created.status_code == 201
    manifest = created.json()
    run_id = manifest["run_id"]
    capture_id = manifest["capture_id"]
    assert client.get(f"/api/captures/{run_id}").status_code == 200

    app.state.database.set_capture_status(capture_id, "pending")
    assert client.get(f"/api/captures/{run_id}").status_code == 404
    app.state.database.set_capture_status(capture_id, "complete")

    manifest_path = capture_root / run_id / "manifest.json"
    manifest_path.write_text(manifest_path.read_text() + " ", encoding="utf-8")
    assert client.get(f"/api/captures/{run_id}").status_code == 404


def test_artifact_delivery_requires_complete_authoritative_registration(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    run_id = "authority-route"
    run_dir = capture_root / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / "evidence.json"
    expected = b'{"exact":true}\n'
    path.write_bytes(expected)
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    capture_id = app.state.database.create_capture(
        run_id=run_id,
        captured_at="2026-07-23T12:00:00+00:00",
        capture_type="can",
        label="authority route",
        backend="test",
    )
    registration = {
        "kind": "evidence",
        "filename": path.name,
        "relative_path": f"{run_id}/{path.name}",
        "media_type": "application/json",
        "size_bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }
    client = TestClient(app)

    assert client.get(f"/artifacts/{run_id}/{path.name}").status_code == 404
    app.state.database.complete_capture_with_artifacts(capture_id, [registration])
    response = client.get(f"/artifacts/{run_id}/{path.name}")
    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"/artifacts/{run_id}/../evidence.json").status_code == 404
    assert client.get(f"/artifacts/{run_id}/nested/evidence.json").status_code == 404

    path.write_bytes(b'{"altered":true}\n')
    assert client.get(f"/artifacts/{run_id}/{path.name}").status_code == 404


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


def test_can_decode_api_lists_sources_creates_child_and_returns_bounded_rows(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
    )
    client = TestClient(app)
    source = client.post("/api/captures", json={
        "label": "Synthetic CAN decode source",
        "preset": "can-analysis",
        "mode": "simulator",
        "capture_type": "can",
        "profile": "network",
    })
    assert source.status_code == 201
    source_manifest = source.json()

    eligible = client.get("/api/can-decode-sources")
    created = client.post("/api/can-decodes", json={
        "source_run_id": source_manifest["run_id"],
        "label": "API decode child",
    })

    assert eligible.status_code == 200
    assert [item["run_id"] for item in eligible.json()["sources"]] == [source_manifest["run_id"]]
    assert eligible.json()["writes_enabled"] is False
    assert created.status_code == 201
    child = created.json()
    assert child["capture_type"] == "can_decode"
    assert child["source_run_id"] == source_manifest["run_id"]
    assert child["writes_performed"] == 0

    result = client.get(f"/api/can-decodes/{child['run_id']}")
    assert result.status_code == 200
    payload = result.json()
    assert payload["total_frame_count"] == child["frame_count"]
    assert payload["returned_frame_count"] == len(payload["frames"])
    assert payload["returned_frame_count"] <= payload["frame_limit"] == 200
    assert payload["frames_truncated"] is (child["frame_count"] > 200)
    assert payload["artifact_urls"] == {
        "frames_jsonl": f"/artifacts/{child['run_id']}/frames.jsonl",
        "identifiers_csv": f"/artifacts/{child['run_id']}/identifiers.csv",
    }
    with (tmp_path / "captures" / child["run_id"] / "frames.jsonl").open("a") as handle:
        handle.write("{}\n")
    assert client.get(f"/api/can-decodes/{child['run_id']}").status_code == 404


@pytest.mark.parametrize(
    "mutation",
    ("waveform_file", "manifest_file", "capture_type", "profile", "artifact_hash"),
)
def test_can_decode_result_revalidates_authoritative_parent_chain(
    tmp_path: Path,
    mutation: str,
) -> None:
    capture_root = tmp_path / "captures"
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)
    source = client.post("/api/captures", json={
        "label": "parent revalidation source",
        "preset": "can-analysis",
        "mode": "simulator",
        "capture_type": "can",
        "profile": "network",
    }).json()
    child_response = client.post("/api/can-decodes", json={
        "source_run_id": source["run_id"],
        "label": "parent revalidation child",
    })
    assert child_response.status_code == 201
    child = child_response.json()
    assert client.get(f"/api/can-decodes/{child['run_id']}").status_code == 200
    source_id = int(source["capture_id"])
    if mutation == "waveform_file":
        with (capture_root / source["run_id"] / "capture.csv").open("a") as handle:
            handle.write("0,0,0,0\n")
    elif mutation == "manifest_file":
        with (capture_root / source["run_id"] / "manifest.json").open("a") as handle:
            handle.write(" ")
    elif mutation == "capture_type":
        with app.state.database._connect() as connection:
            connection.execute("UPDATE captures SET capture_type = 'serial' WHERE id = ?", (source_id,))
    elif mutation == "profile":
        app.state.database.set_capture_metadata(source_id, {"profile": "general"})
    else:
        with app.state.database._connect() as connection:
            connection.execute(
                "UPDATE artifacts SET sha256 = ? WHERE capture_id = ? AND filename = 'capture.csv'",
                ("0" * 64, source_id),
            )
    assert client.get(f"/api/can-decodes/{child['run_id']}").status_code == 404


def test_can_decode_source_listing_requires_complete_matching_sqlite_rows(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)
    source = client.post("/api/captures", json={
        "label": "authoritative source",
        "preset": "can-analysis",
        "mode": "simulator",
        "capture_type": "can",
        "profile": "network",
    }).json()
    source_id = int(source["capture_id"])
    orphan_dir = capture_root / "orphan-source"
    shutil.copytree(capture_root / source["run_id"], orphan_dir)
    orphan_manifest = json.loads((orphan_dir / "manifest.json").read_text())
    orphan_manifest["run_id"] = "orphan-source"
    (orphan_dir / "manifest.json").write_text(json.dumps(orphan_manifest))

    assert [item["run_id"] for item in client.get("/api/can-decode-sources").json()["sources"]] == [
        source["run_id"]
    ]
    app.state.database.set_capture_status(source_id, "pending")
    assert client.get("/api/can-decode-sources").json()["sources"] == []
    with app.state.database._connect() as connection:
        connection.execute(
            "UPDATE captures SET status = 'complete', capture_type = 'serial' WHERE id = ?",
            (source_id,),
        )
    assert client.get("/api/can-decode-sources").json()["sources"] == []


def test_can_decode_source_listing_allows_historical_bus_survey_null_profile_only(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)
    survey_response = client.post("/api/bus-surveys", json={
        "label": "historical CAN survey",
        "harness": "can-network",
        "mode": "simulator",
    })
    assert survey_response.status_code == 201
    survey = survey_response.json()
    app.state.database.set_capture_metadata(int(survey["capture_id"]), {})

    direct_response = client.post("/api/captures", json={
        "label": "direct CAN null profile",
        "preset": "can-analysis",
        "mode": "simulator",
        "capture_type": "can",
        "profile": "network",
    })
    assert direct_response.status_code == 201
    direct = direct_response.json()
    app.state.database.set_capture_metadata(int(direct["capture_id"]), {})

    sources = client.get("/api/can-decode-sources").json()["sources"]
    run_ids = {source["run_id"] for source in sources}
    assert survey["run_id"] in run_ids
    assert direct["run_id"] not in run_ids


def test_can_decode_api_maps_malformed_missing_and_busy_requests(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "captures", db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)

    assert client.post("/api/can-decodes", json={
        "source_run_id": "../escape", "label": "bad",
    }).status_code == 422
    assert client.post("/api/can-decodes", json={
        "source_run_id": "missing-source", "label": "missing",
    }).status_code == 404
    assert client.get("/api/can-decodes/../escape").status_code == 404

    app.state.can_decode_manager._lock.acquire()
    try:
        response = client.post("/api/can-decodes", json={
            "source_run_id": "missing-source", "label": "busy",
        })
    finally:
        app.state.can_decode_manager._lock.release()
    assert response.status_code == 409


def test_can_decode_openapi_is_passive_and_has_no_bus_authority(tmp_path: Path) -> None:
    schema = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
    ).openapi()
    can_paths = {
        path: methods for path, methods in schema["paths"].items()
        if "can-decode" in path
    }
    assert set(can_paths) == {
        "/api/can-decode-sources",
        "/api/can-decodes",
        "/api/can-decodes/{run_id}",
    }
    rendered = str(can_paths).lower()
    for forbidden in ("transmit", "replay", "socketcan", "ack_generation", "write_payload"):
        assert forbidden not in rendered


def test_can_decode_filter_scans_full_artifact_before_applying_api_limit(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    run_id = "synthetic-child-can-decode"
    run_dir = capture_root / run_id
    run_dir.mkdir(parents=True)
    app = create_app(
        data_dir=capture_root,
        db_path=tmp_path / "evidence.sqlite3",
    )
    source_identity = _register_authoritative_can_parent(
        app, capture_root, run_id="synthetic-source", samples=3_000,
    )
    frames = [
        {
            "identifier": 0x100,
            "identifier_hex": "0x100",
            "extended": False,
            "timestamp_us": float(index),
            "remote": False,
            "dlc": 1,
            "payload_bytes": [0],
            "payload_hex": "00",
            "crc_valid": True,
            "ack_slot": "dominant",
            "nominal_bitrate_bps": 500_000,
            "source_sample_start": index * 10,
            "source_sample_end": index * 10 + 9,
        }
        for index in range(204)
    ]
    frames.extend([{
        "identifier": 0x321,
        "identifier_hex": "0x321",
        "extended": False,
        "timestamp_us": float(204 + offset),
        "remote": False,
        "dlc": 1,
        "payload_bytes": [value],
        "payload_hex": f"{value:02X}",
        "crc_valid": True,
        "ack_slot": "recessive",
        "nominal_bitrate_bps": 500_000,
        "source_sample_start": (204 + offset) * 10,
        "source_sample_end": (204 + offset) * 10 + 9,
    } for offset, value in enumerate((0xAA, 0xBB))])
    (run_dir / "frames.jsonl").write_text(
        "".join(json.dumps(frame) + "\n" for frame in frames),
        encoding="utf-8",
    )
    identifiers = aggregate_can_identifiers(frames)
    document_identity = {
        "run_id": run_id,
        "capture_type": "can_decode",
        **source_identity,
        "can_polarity": "expected",
        "nominal_bitrate_bps": 500_000,
        "writes_performed": 0,
    }
    (run_dir / "summary.json").write_text(json.dumps({
        **document_identity,
        "frame_count": len(frames),
        "identifier_count": len(identifiers),
        "identifiers": identifiers,
    }))
    (run_dir / "manifest.json").write_text(json.dumps({
        **document_identity,
        "frame_count": len(frames),
        "identifier_count": len(identifiers),
    }))
    capture_id = app.state.database.create_capture(
        run_id=run_id,
        captured_at="2026-07-23T12:00:00+00:00",
        capture_type="can_decode",
        label="synthetic child",
        backend="test",
    )
    registrations = []
    for filename in ("frames.jsonl", "identifiers.csv", "summary.json", "manifest.json"):
        path = run_dir / filename
        if not path.exists():
            path.write_text("", encoding="utf-8")
        registrations.append({
            "kind": "can_decode",
            "filename": filename,
            "relative_path": f"{run_id}/{filename}",
            "media_type": "application/json",
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    client = TestClient(app)
    assert client.get(f"/api/can-decodes/{run_id}").status_code == 404
    app.state.database.complete_capture_with_artifacts(capture_id, registrations)

    unfiltered = client.get(f"/api/can-decodes/{run_id}").json()
    filtered = client.get(f"/api/can-decodes/{run_id}?identifier=0x321").json()
    changing = client.get(f"/api/can-decodes/{run_id}?changing_only=true").json()

    assert unfiltered["returned_frame_count"] == 200
    assert unfiltered["total_frame_count"] == 206
    assert unfiltered["frames_truncated"] is True
    assert [frame["identifier_hex"] for frame in filtered["frames"]] == ["0x321", "0x321"]
    assert filtered["total_frame_count"] == filtered["returned_frame_count"] == 2
    assert [item["identifier_hex"] for item in changing["identifiers"]] == ["0x321"]


def test_capture_listing_publishes_only_complete_verified_database_manifests(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)
    created = client.post("/api/captures", json={
        "label": "publication boundary",
        "preset": "short",
        "mode": "simulator",
    }).json()
    capture_id = int(created["capture_id"])

    app.state.database.set_capture_status(capture_id, "pending")
    assert created["run_id"] not in {
        item["run_id"] for item in client.get("/api/captures").json()
    }
    app.state.database.set_capture_status(capture_id, "complete")
    assert created["run_id"] in {
        item["run_id"] for item in client.get("/api/captures").json()
    }

    manifest_path = capture_root / created["run_id"] / "manifest.json"
    manifest_path.write_text(manifest_path.read_text() + " ", encoding="utf-8")
    assert created["run_id"] not in {
        item["run_id"] for item in client.get("/api/captures").json()
    }


def test_can_decode_source_listing_omits_authoritative_non_object_manifest(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "captures"
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)
    created = client.post("/api/captures", json={
        "label": "non-object source manifest",
        "preset": "can-analysis",
        "mode": "simulator",
        "capture_type": "can",
        "profile": "network",
    }).json()
    manifest_path = capture_root / created["run_id"] / "manifest.json"
    manifest_path.write_text("[]\n", encoding="utf-8")
    with app.state.database._connect() as connection:
        connection.execute(
            """
            UPDATE artifacts SET size_bytes = ?, sha256 = ?
            WHERE capture_id = ? AND filename = 'manifest.json'
            """,
            (
                manifest_path.stat().st_size,
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                created["capture_id"],
            ),
        )

    response = client.get("/api/can-decode-sources")
    assert response.status_code == 200
    assert created["run_id"] not in {
        item["run_id"] for item in response.json()["sources"]
    }


@pytest.mark.parametrize(
    "failure",
    ("summary_bytes", "frames_bytes", "line_count", "line_bytes", "malformed_frame"),
)
def test_can_decode_result_artifact_bounds_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    capture_root = tmp_path / "captures"
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    client = TestClient(app)
    source = client.post("/api/captures", json={
        "label": "bounded CAN source",
        "preset": "can-analysis",
        "mode": "simulator",
        "capture_type": "can",
        "profile": "network",
    }).json()
    child = client.post("/api/can-decodes", json={
        "source_run_id": source["run_id"],
        "label": "bounded child",
    }).json()
    run_dir = capture_root / child["run_id"]
    target = run_dir / ("summary.json" if failure == "summary_bytes" else "frames.jsonl")
    if failure == "summary_bytes":
        monkeypatch.setattr(app_module, "MAX_SUMMARY_BYTES", target.stat().st_size - 1)
    elif failure == "frames_bytes":
        monkeypatch.setattr(app_module, "MAX_FRAMES_JSONL_BYTES", target.stat().st_size - 1)
    elif failure == "line_count":
        monkeypatch.setattr(
            app_module, "MAX_SCANNED_FRAME_LINES",
            len(target.read_bytes().splitlines()) - 1,
        )
    elif failure == "line_bytes":
        monkeypatch.setattr(
            app_module, "MAX_FRAME_LINE_BYTES",
            max(len(line) for line in target.read_bytes().splitlines()) - 1,
        )
    else:
        lines = target.read_bytes().splitlines()
        lines[0] = b"{}"
        target.write_bytes(b"\n".join(lines) + b"\n")
        with app.state.database._connect() as connection:
            connection.execute(
                """
                UPDATE artifacts SET size_bytes = ?, sha256 = ?
                WHERE capture_id = ? AND filename = 'frames.jsonl'
                """,
                (
                    target.stat().st_size,
                    hashlib.sha256(target.read_bytes()).hexdigest(),
                    child["capture_id"],
                ),
            )

    response = client.get(f"/api/can-decodes/{child['run_id']}")
    assert response.status_code == 404
    assert "frames" not in response.json()


@pytest.mark.parametrize(
    "mutation",
    (
        "summary_identifier_range", "summary_identifier_hex", "summary_frame_count",
        "summary_timestamps", "summary_payload_changes", "summary_last_payload",
        "summary_byte_changes", "summary_interval",
        "frame_identifier_range", "frame_identifier_hex", "frame_remote",
        "frame_dlc", "frame_payload_byte", "frame_payload_hex", "frame_payload_length",
        "frame_crc", "frame_ack", "frame_bitrate", "frame_timestamp",
        "frame_indices", "chronology", "missing_summary_key", "per_key_count",
        "total_frame_count", "identifier_count", "document_identity",
        "document_polarity", "document_bitrate", "document_writes",
        "document_counts_missing", "frame_bitrate_mismatch", "frame_source_bound",
        "frame_source_order", "summary_first_consistency", "summary_payload_consistency",
        "summary_last_payload_consistency", "summary_byte_consistency",
        "summary_interval_consistency",
    ),
)
def test_can_decode_result_strict_schema_and_consistency_fail_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    capture_root = tmp_path / "captures"
    run_id = f"strict-{mutation.replace('_', '-')}"
    run_dir = capture_root / run_id
    run_dir.mkdir(parents=True)
    app = create_app(data_dir=capture_root, db_path=tmp_path / "evidence.sqlite3")
    source_identity = _register_authoritative_can_parent(
        app, capture_root, run_id="authoritative-source", samples=1_000,
    )
    frames = [
        {
            "identifier": 0x321,
            "identifier_hex": "0x321",
            "extended": False,
            "remote": False,
            "dlc": 1,
            "payload_bytes": [value],
            "payload_hex": f"{value:02X}",
            "crc_valid": True,
            "ack_slot": "recessive",
            "nominal_bitrate_bps": 500_000,
            "timestamp_us": float(index),
            "source_sample_start": index * 10,
            "source_sample_end": index * 10 + 9,
        }
        for index, value in enumerate((0xAA, 0xBB), start=1)
    ]
    identifiers = aggregate_can_identifiers(frames)
    document_identity = {
        "run_id": run_id,
        "capture_type": "can_decode",
        **source_identity,
        "can_polarity": "expected",
        "nominal_bitrate_bps": 500_000,
        "writes_performed": 0,
    }
    summary = {
        **document_identity,
        "frame_count": 2,
        "identifier_count": 1,
        "identifiers": copy.deepcopy(identifiers),
    }
    manifest = {
        **document_identity,
        "frame_count": 2,
        "identifier_count": 1,
    }
    item = summary["identifiers"][0]
    frame = frames[0]
    if mutation == "summary_identifier_range":
        item["identifier"] = 0x800
    elif mutation == "summary_identifier_hex":
        item["identifier_hex"] = "0x321".lower().replace("x", "X")
    elif mutation == "summary_frame_count":
        item["frame_count"] = 0
    elif mutation == "summary_timestamps":
        item["last_timestamp_us"] = -1
    elif mutation == "summary_payload_changes":
        item["payload_change_count"] = 2
    elif mutation == "summary_last_payload":
        item["last_payload_hex"] = "bb"
    elif mutation == "summary_byte_changes":
        item["byte_change_counts"] = [-1]
    elif mutation == "summary_interval":
        item["mean_period_us"] = float("inf")
    elif mutation == "frame_identifier_range":
        frame["identifier"] = 0x800
    elif mutation == "frame_identifier_hex":
        frame["identifier_hex"] = "0x0321"
    elif mutation == "frame_remote":
        frame["remote"] = "false"
    elif mutation == "frame_dlc":
        frame["dlc"] = True
    elif mutation == "frame_payload_byte":
        frame["payload_bytes"] = [256]
    elif mutation == "frame_payload_hex":
        frame["payload_hex"] = "aa"
    elif mutation == "frame_payload_length":
        frame["dlc"] = 2
    elif mutation == "frame_crc":
        frame["crc_valid"] = False
    elif mutation == "frame_ack":
        frame["ack_slot"] = "unknown"
    elif mutation == "frame_bitrate":
        frame["nominal_bitrate_bps"] = 123_456
    elif mutation == "frame_timestamp":
        frame["timestamp_us"] = -1
    elif mutation == "frame_indices":
        frame["source_sample_start"] = True
    elif mutation == "chronology":
        frames[1]["timestamp_us"] = 0.0
    elif mutation == "missing_summary_key":
        frame["identifier"] = 0x123
        frame["identifier_hex"] = "0x123"
    elif mutation == "per_key_count":
        item["frame_count"] = 3
    elif mutation == "total_frame_count":
        manifest["frame_count"] = 3
    elif mutation == "identifier_count":
        summary["identifier_count"] = 2
    elif mutation == "document_identity":
        summary["source_run_id"] = "different-source"
    elif mutation == "document_polarity":
        manifest["can_polarity"] = "sideways"
    elif mutation == "document_bitrate":
        summary["nominal_bitrate_bps"] = 123_456
    elif mutation == "document_writes":
        manifest["writes_performed"] = 1
    elif mutation == "document_counts_missing":
        manifest.pop("frame_count")
        summary.pop("identifier_count")
    elif mutation == "frame_bitrate_mismatch":
        frame["nominal_bitrate_bps"] = 250_000
    elif mutation == "frame_source_bound":
        frame["source_sample_end"] = 1_001
    elif mutation == "frame_source_order":
        frames[1]["source_sample_start"] = 5
        frames[1]["source_sample_end"] = 8
    elif mutation == "summary_first_consistency":
        item["first_timestamp_us"] = 0.0
    elif mutation == "summary_payload_consistency":
        item["payload_change_count"] = 0
    elif mutation == "summary_last_payload_consistency":
        item["last_payload_hex"] = "AA"
    elif mutation == "summary_byte_consistency":
        item["byte_change_counts"] = [0]
    elif mutation == "summary_interval_consistency":
        item["mean_period_us"] = 2.0

    files = {
        "frames.jsonl": "".join(json.dumps(value) + "\n" for value in frames),
        "identifiers.csv": "identifier\n0x321\n",
        "summary.json": json.dumps(summary),
        "manifest.json": json.dumps(manifest),
    }
    for filename, content in files.items():
        (run_dir / filename).write_text(content, encoding="utf-8")
    capture_id = app.state.database.create_capture(
        run_id=run_id,
        captured_at="2026-07-23T12:00:00+00:00",
        capture_type="can_decode",
        label="strict schema",
        backend="test",
    )
    registrations = []
    for filename in files:
        path = run_dir / filename
        registrations.append({
            "kind": "can_decode",
            "filename": filename,
            "relative_path": f"{run_id}/{filename}",
            "media_type": "application/json",
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    app.state.database.complete_capture_with_artifacts(capture_id, registrations)

    response = TestClient(app).get(f"/api/can-decodes/{run_id}")
    assert response.status_code == 404
    assert "frames" not in response.json()


def test_api_creates_and_lists_customer_vehicle_and_diagnostic_session(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
    )
    client = TestClient(app)

    customer = client.post(
        "/api/customers",
        json={
            "name": "Example Customer",
            "company": "Example Fleet",
            "phone": "555-0100",
            "email": "service@example.invalid",
        },
    )
    vehicle = client.post(
        "/api/vehicles",
        json={
            "display_name": "2009 Subaru Forester",
            "vin": "RDLTEST0000000003",
            "make": "Subaru",
            "model": "Forester",
            "year": 2009,
        },
    )
    session = client.post(
        "/api/diagnostic-sessions",
        json={
            "customer_id": customer.json()["id"],
            "vehicle_id": vehicle.json()["id"],
            "title": "OBD scan",
            "purpose": "Read emissions data",
            "complaint": "MIL history",
            "operator_name": "Daniel",
        },
    )

    assert customer.status_code == 201
    assert vehicle.status_code == 201
    assert session.status_code == 201
    assert client.get("/api/customers").json()[0]["name"] == "Example Customer"
    assert client.get("/api/vehicles").json()[0]["vin"] == "RDLTEST0000000003"
    listed_sessions = client.get("/api/diagnostic-sessions").json()
    assert listed_sessions[0]["session_id"] == session.json()["session_id"]
    assert listed_sessions[0]["customer"]["name"] == "Example Customer"
    assert listed_sessions[0]["vehicle"]["display_name"] == "2009 Subaru Forester"


def test_api_rejects_whitespace_only_customer_name(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
    )
    client = TestClient(app)

    response = client.post("/api/customers", json={"name": "   "})

    assert response.status_code == 422
    assert "customer name is required" in response.text


def test_api_rejects_whitespace_only_vehicle_name_as_validation_error(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "captures", db_path=tmp_path / "db.sqlite3")

    response = TestClient(app).post("/api/vehicles", json={"display_name": "   "})

    assert response.status_code == 422
    assert "vehicle display name is required" in response.text


def test_artifact_mount_does_not_serve_partial_or_quarantined_evidence(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    client = TestClient(create_app(data_dir=captures, db_path=tmp_path / "db.sqlite3"))
    partial = captures / ".obd-known.partial"
    quarantined = captures / ".orphaned-obd" / "obd-known"
    uncommitted = captures / "obd-uncommitted"
    partial.mkdir(parents=True)
    quarantined.mkdir(parents=True)
    uncommitted.mkdir(parents=True)
    (partial / "obd-snapshot.json").write_text("partial", encoding="utf-8")
    (quarantined / "manifest.json").write_text("orphan", encoding="utf-8")
    (uncommitted / "manifest.json").write_text("not committed", encoding="utf-8")

    assert client.get("/artifacts/.obd-known.partial/obd-snapshot.json").status_code == 404
    assert client.get("/artifacts/.orphaned-obd/obd-known/manifest.json").status_code == 404
    assert client.get("/artifacts/obd-uncommitted/manifest.json").status_code == 404


def test_app_shutdown_closes_active_obd_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "evidence.sqlite3"
    app = create_app(data_dir=tmp_path / "captures", db_path=db_path)

    with TestClient(app) as client:
        connected = client.post(
            "/api/obd/connect",
            json={"mode": "simulator", "session_id": None},
        ).json()

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, ended_at FROM obd_connections WHERE id = ?",
            (connected["connection_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "closed"
    assert row[1] is not None


def test_api_obd_simulator_reads_and_persists_session_artifact(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "evidence.sqlite3",
    )
    client = TestClient(app)
    customer_id = client.post("/api/customers", json={"name": "Bench"}).json()["id"]
    vehicle_id = client.post(
        "/api/vehicles", json={"display_name": "Simulator Vehicle"}
    ).json()["id"]
    session_id = client.post(
        "/api/diagnostic-sessions",
        json={
            "customer_id": customer_id,
            "vehicle_id": vehicle_id,
            "title": "OBD simulator",
            "purpose": "API proof",
        },
    ).json()["session_id"]

    assert client.get("/api/obd/status").json()["connected"] is False
    connected = client.post(
        "/api/obd/connect",
        json={"mode": "simulator", "session_id": session_id},
    )
    live = client.get("/api/obd/live")
    faults = client.get("/api/obd/faults")
    vehicle = client.get("/api/obd/vehicle-info")
    saved = client.post(
        "/api/obd/snapshots",
        json={
            "kind": "faults",
            "label": "Baseline emissions scan",
            "operation_id": "00000000-0000-4000-8000-000000000003",
        },
    )
    saved_retry = client.post(
        "/api/obd/snapshots",
        json={
            "kind": "faults",
            "label": "Baseline emissions scan",
            "operation_id": "00000000-0000-4000-8000-000000000003",
        },
    )

    assert connected.status_code == 201
    assert connected.json()["provider"] == "obd-simulator"
    assert live.status_code == 200
    assert any(item["pid"] == "0C" for item in live.json()["values"])
    assert [item["code"] for item in faults.json()["stored"]] == [
        "P0102", "P0113", "P0028",
    ]
    assert vehicle.json()["vins"][0]["vin"] == "RDLTEST1234567890"
    assert saved.status_code == 201
    manifest = saved.json()
    assert saved_retry.status_code == 201
    assert saved_retry.json()["run_id"] == manifest["run_id"]
    assert manifest["capture_type"] == "obd_scan"
    assert manifest["profile"] == "obd"
    assert set(manifest["artifacts"]) == {"manifest.json", "obd-snapshot.json"}
    evidence = client.get(
        f"/api/evidence/captures/{manifest['capture_id']}"
    ).json()
    assert len(evidence["artifacts"]) == 2
    snapshots = client.get(
        f"/api/diagnostic-sessions/{session_id}/obd-snapshots"
    ).json()
    assert snapshots[0]["capture_id"] == manifest["capture_id"]
    assert client.get(
        f"/artifacts/{manifest['run_id']}/obd-snapshot.json"
    ).status_code == 200
    with sqlite3.connect(tmp_path / "evidence.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM captures WHERE run_id = ?",
            (manifest["run_id"],),
        ).fetchone()[0] == 1
    assert client.post("/api/obd/disconnect").json()["connected"] is False


def test_api_obd_rejects_unknown_diagnostic_session_without_staying_connected(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=tmp_path / "remote-dan.sqlite3",
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/obd/connect",
        json={"mode": "simulator", "session_id": 999},
    )

    assert response.status_code == 422
    assert "diagnostic session does not exist" in response.json()["detail"]
    assert client.get("/api/obd/status").json()["connected"] is False


def test_api_obd_clear_prepare_fails_closed_without_authenticated_operator(
    tmp_path: Path,
) -> None:
    app = create_app(data_dir=tmp_path / "captures", db_path=tmp_path / "db.sqlite3")
    client = TestClient(app)
    client.post("/api/obd/connect", json={"mode": "simulator"})

    response = client.post("/api/obd/faults/clear/prepare")

    assert response.status_code == 403
    assert "authenticated operator" in response.json()["detail"]


def test_api_obd_connect_defaults_to_hardware_not_simulator(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "captures", db_path=tmp_path / "db.sqlite3")
    client = TestClient(app)

    response = client.post("/api/obd/connect", json={})

    assert response.status_code == 502
    assert "hardware" in response.json()["detail"].lower()
    assert client.get("/api/obd/status").json()["connected"] is False


def test_adapter_terminal_failure_is_http_502_and_cannot_create_complete_evidence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = create_app(
        data_dir=tmp_path / "captures",
        db_path=db_path,
        obd_simulator_provider=AdapterFailingLiveProvider(),
    )
    client = TestClient(app)
    customer_id = client.post("/api/customers", json={"name": "Bench"}).json()["id"]
    vehicle_id = client.post("/api/vehicles", json={"display_name": "Vehicle"}).json()["id"]
    session_id = client.post(
        "/api/diagnostic-sessions",
        json={
            "customer_id": customer_id,
            "vehicle_id": vehicle_id,
            "title": "Adapter failure",
            "purpose": "Regression",
        },
    ).json()["session_id"]
    assert client.post(
        "/api/obd/connect", json={"mode": "simulator", "session_id": session_id}
    ).status_code == 201

    live = client.get("/api/obd/live")
    saved = client.post(
        "/api/obd/snapshots",
        json={
            "kind": "live",
            "label": "must fail",
            "operation_id": "00000000-0000-4000-8000-000000000099",
        },
    )

    assert live.status_code == 502
    assert saved.status_code == 502
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM obd_snapshots").fetchone()[0] == 0
