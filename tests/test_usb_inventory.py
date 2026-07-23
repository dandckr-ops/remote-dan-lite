from __future__ import annotations

from pathlib import Path

from remote_dan.usb_inventory import list_usb_devices


def _usb_device(root: Path, name: str, *, vendor: str, product: str, serial: str = "", product_name: str = "") -> Path:
    device = root / name
    device.mkdir(parents=True)
    (device / "idVendor").write_text(vendor, encoding="utf-8")
    (device / "idProduct").write_text(product, encoding="utf-8")
    if serial:
        (device / "serial").write_text(serial, encoding="utf-8")
    if product_name:
        (device / "product").write_text(product_name, encoding="utf-8")
    return device


def test_inventory_reports_stable_identity_without_guessing_route(tmp_path: Path) -> None:
    _usb_device(
        tmp_path,
        "1-2.3",
        vendor="0CE9\n",
        product="1016\n",
        serial="PQ123/456\n",
        product_name="PicoScope 2406B\n",
    )

    devices = list_usb_devices(tmp_path)

    assert devices == [{
        "key": "usb:0ce9:1016:PQ123/456:1-2.3",
        "vendor_id": "0ce9",
        "product_id": "1016",
        "serial": "PQ123/456",
        "product_name": "PicoScope 2406B",
        "topology_path": "1-2.3",
        "route": "unknown",
    }]


def test_inventory_uses_topology_when_device_has_no_serial(tmp_path: Path) -> None:
    _usb_device(tmp_path, "1-1", vendor="10c4", product="ea60", product_name="CP210x UART Bridge")

    devices = list_usb_devices(tmp_path)

    assert devices[0]["key"] == "usb:10c4:ea60:-:1-1"
    assert devices[0]["serial"] is None
    assert devices[0]["route"] == "unknown"
