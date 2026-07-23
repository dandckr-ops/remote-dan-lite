from __future__ import annotations

from pathlib import Path

import pytest

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


def test_apply_rejects_stale_inventory_without_touching_virtualhere_config(tmp_path: Path) -> None:
    config = tmp_path / "config.ini"
    original = "TCPPort=7575\nAllowedDevices=084f/c050\n"
    config.write_text(original, encoding="utf-8")
    initial = [device("usb:084f:c050:ec-1:1-1", "084f", "c050")]
    changed = [device("usb:1adb:0001:sel-1:1-2", "1adb", "0001")]
    restarts: list[str] = []
    helper = RoutingHelper(
        config_path=config,
        inventory_probe=lambda: changed,
        restart=lambda: restarts.append("restart"),
        is_active=lambda: True,
    )

    with pytest.raises(RoutingHelperError, match="inventory changed"):
        helper.apply(
            expected_inventory_revision=inventory_revision(initial),
            routes={initial[0]["key"]: "local"},
        )

    assert config.read_text(encoding="utf-8") == original
    assert restarts == []


def test_apply_changes_only_explicitly_selected_unique_device(tmp_path: Path) -> None:
    config = tmp_path / "config.ini"
    config.write_text("TCPPort=7575\nAllowedDevices=084f/c050\n", encoding="utf-8")
    ecom = device("usb:084f:c050:ec-1:1-1", "084f", "c050")
    sel = device("usb:1adb:0001:sel-1:1-2", "1adb", "0001")
    restarts: list[str] = []
    helper = RoutingHelper(
        config_path=config,
        inventory_probe=lambda: [ecom, sel],
        restart=lambda: restarts.append("restart"),
        is_active=lambda: True,
    )

    result = helper.apply(
        expected_inventory_revision=inventory_revision([ecom, sel]),
        routes={sel["key"]: "virtualhere"},
    )

    assert result["allowed_devices"] == ["1adb/0001"]
    assert config.read_text(encoding="utf-8") == "TCPPort=7575\nAllowedDevices=1adb/0001\n"
    assert restarts == ["restart"]
