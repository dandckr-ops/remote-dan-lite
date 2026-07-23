from __future__ import annotations

import ctypes.util
from pathlib import Path
import platform
from typing import Any

PICO_USB_VENDOR_ID = "0ce9"


def _pico_usb_present() -> bool:
    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.exists():
        return False
    for vendor_file in usb_root.glob("*/idVendor"):
        try:
            if vendor_file.read_text().strip().lower() == PICO_USB_VENDOR_ID:
                return True
        except OSError:
            continue
    return False


def probe_pico_hardware() -> dict[str, Any]:
    architecture = platform.machine()
    driver = ctypes.util.find_library("ps2000a")
    device_present = _pico_usb_present()
    if not driver and architecture in {"aarch64", "arm64"}:
        reason = (
            "Native libps2000a is not installed. Pico's current Early Access "
            "repository provides an ARM64 driver package for this host."
        )
    elif not driver:
        reason = "Native libps2000a was not found."
    elif not device_present:
        reason = "The PS2000A driver is installed, but no Pico USB device is attached."
    else:
        reason = "Pico driver and USB device detected."
    return {
        "architecture": architecture,
        "driver_available": bool(driver),
        "driver_library": driver,
        "device_present": device_present,
        "reason": reason,
    }
