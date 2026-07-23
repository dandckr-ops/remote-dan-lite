from __future__ import annotations

import json
from pathlib import Path

import pytest

from remote_dan.routing_helper_service import (
    CONFIG_PATH,
    VIRTUALHERE_SERVICE,
    RoutingHelperService,
    build_root_helper,
    systemctl_is_active,
    systemctl_restart,
)
from remote_dan.virtualhere_helper import RoutingHelper, RoutingHelperError, inventory_revision


def device(key: str, vendor_id: str, product_id: str) -> dict[str, str | None]:
    return {
        "key": key,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "serial": "unique",
        "product_name": "test device",
        "topology_path": "1-1",
        "route": "unknown",
    }


def make_service(tmp_path: Path, inventory: list[dict[str, str | None]]) -> tuple[RoutingHelperService, Path]:
    config = tmp_path / "config.ini"
    config.write_text("TCPPort=7575\nAllowedDevices=084f/c050\n", encoding="utf-8")
    helper = RoutingHelper(
        config_path=config,
        inventory_probe=lambda: inventory,
        restart=lambda: None,
        is_active=lambda: True,
    )
    return RoutingHelperService(helper, audit_path=tmp_path / "state" / "routing-audit.jsonl"), config


def test_apply_writes_durable_old_to_new_success_audit_record(tmp_path: Path) -> None:
    ecom = device("usb:084f:c050:ec-1:1-1", "084f", "c050")
    sel = device("usb:1adb:0001:sel-1:1-2", "1adb", "0001")
    service, _ = make_service(tmp_path, [ecom, sel])

    response = service.handle(
        {
            "action": "apply",
            "inventory_revision": inventory_revision([ecom, sel]),
            "routes": {sel["key"]: "virtualhere"},
        }
    )

    records = [json.loads(line) for line in service.audit_path.read_text(encoding="utf-8").splitlines()]
    assert response["allowed_devices"] == ["1adb/0001"]
    assert records == [
        {
            "action": "apply",
            "new_allowed_devices": ["1adb/0001"],
            "old_allowed_devices": ["084f/c050"],
            "outcome": "success",
            "timestamp": records[0]["timestamp"],
        }
    ]
    assert records[0]["timestamp"].endswith("+00:00")


def test_failed_apply_audits_unchanged_old_to_new_and_error(tmp_path: Path) -> None:
    current = [device("usb:1adb:0001:sel-1:1-2", "1adb", "0001")]
    stale = [device("usb:084f:c050:ec-1:1-1", "084f", "c050")]
    service, config = make_service(tmp_path, current)

    with pytest.raises(RoutingHelperError, match="inventory changed"):
        service.handle(
            {
                "action": "apply",
                "inventory_revision": inventory_revision(stale),
                "routes": {},
            }
        )

    record = json.loads(service.audit_path.read_text(encoding="utf-8"))
    assert config.read_text(encoding="utf-8") == "TCPPort=7575\nAllowedDevices=084f/c050\n"
    assert record["old_allowed_devices"] == ["084f/c050"]
    assert record["new_allowed_devices"] == ["084f/c050"]
    assert record["outcome"] == "error"
    assert "inventory changed" in record["error"]


def test_status_does_not_create_audit_record(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, [device("usb:084f:c050:ec-1:1-1", "084f", "c050")])

    response = service.handle({"action": "status"})

    assert response["available"] is True
    assert not service.audit_path.exists()


def test_systemctl_adapter_targets_only_virtualhere_basler(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    class Result:
        returncode = 0

    def fake_run(command: list[str], *, check: bool) -> Result:
        calls.append((command, check))
        return Result()

    monkeypatch.setattr("remote_dan.routing_helper_service.subprocess.run", fake_run)

    systemctl_restart()
    assert systemctl_is_active() is True
    helper = build_root_helper()

    assert helper.config_path == CONFIG_PATH
    assert calls == [
        (["systemctl", "restart", VIRTUALHERE_SERVICE], True),
        (["systemctl", "is-active", "--quiet", VIRTUALHERE_SERVICE], False),
    ]


def test_deployed_units_are_socket_activated_root_helper_without_http_listener() -> None:
    root = Path(__file__).parents[1]
    socket_unit = (root / "deploy" / "remote-dan-routing.socket").read_text(encoding="utf-8")
    service_unit = (root / "deploy" / "remote-dan-routing.service").read_text(encoding="utf-8")

    assert "ListenStream=/run/remote-dan-routing/control.sock" in socket_unit
    assert "SocketUser=remotedan" in socket_unit
    assert "SocketGroup=remotedan" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "DirectoryMode=0755" in socket_unit
    assert "User=root" in service_unit
    assert "WorkingDirectory=/opt/remote-dan-lite" in service_unit
    assert "ExecStart=/opt/remote-dan-lite/.venv/bin/python -m remote_dan.routing_helper_service --socket-activation" in service_unit
    assert "--socket-activation" in service_unit
    assert "/etc/virtualhere" in service_unit
    assert "virtualhere-basler.service" in service_unit
    assert "uvicorn" not in service_unit.lower()
    assert "ListenStream" not in service_unit
