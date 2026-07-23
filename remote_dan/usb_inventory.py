from __future__ import annotations

from pathlib import Path

USB_SYSFS_ROOT = Path("/sys/bus/usb/devices")


def _read_value(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def list_usb_devices(root: Path = USB_SYSFS_ROOT) -> list[dict[str, str | None]]:
    """Return local USB identities only; routing state remains unknown until proven."""
    devices: list[dict[str, str | None]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return devices

    for entry in entries:
        vendor_id = _read_value(entry / "idVendor")
        product_id = _read_value(entry / "idProduct")
        if vendor_id is None or product_id is None:
            continue
        vendor_id = vendor_id.lower()
        product_id = product_id.lower()
        serial = _read_value(entry / "serial")
        topology_path = entry.name
        devices.append({
            "key": f"usb:{vendor_id}:{product_id}:{serial or '-'}:{topology_path}",
            "vendor_id": vendor_id,
            "product_id": product_id,
            "serial": serial,
            "product_name": _read_value(entry / "product"),
            "topology_path": topology_path,
            "route": "unknown",
        })
    return devices
