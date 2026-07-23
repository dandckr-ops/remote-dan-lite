from __future__ import annotations

import pytest

from remote_dan.usb_routing import (
    RoutingPolicyError,
    plan_virtualhere_allowlist,
    render_allowed_devices,
)


def device(key: str, vendor_id: str, product_id: str) -> dict[str, str | None]:
    return {
        "key": key,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "serial": None,
        "product_name": "test device",
        "topology_path": key.rsplit(":", 1)[-1],
        "route": "unknown",
    }


def test_plan_allows_a_unique_ecom_device_to_be_forwarded() -> None:
    ecom = device("usb:084f:c050:-:3-1", "084f", "c050")
    pico = device("usb:0ce9:1016:-:1-1", "0ce9", "1016")

    plan = plan_virtualhere_allowlist([ecom, pico], {ecom["key"]: "virtualhere"})

    assert plan.allowed_devices == ("084f/c050",)
    assert plan.routes == {ecom["key"]: "virtualhere", pico["key"]: "local"}


def test_plan_rejects_forwarding_a_protected_local_capture_device() -> None:
    pico = device("usb:0ce9:1016:-:1-1", "0ce9", "1016")

    with pytest.raises(RoutingPolicyError, match="protected local capture device"):
        plan_virtualhere_allowlist([pico], {pico["key"]: "virtualhere"})


def test_plan_rejects_ambiguous_duplicate_vid_pid_selection() -> None:
    first = device("usb:084f:c050:-:3-1", "084f", "c050")
    second = device("usb:084f:c050:-:3-2", "084f", "c050")

    with pytest.raises(RoutingPolicyError, match="cannot select one physical device"):
        plan_virtualhere_allowlist([first, second], {first["key"]: "virtualhere"})


def test_render_allowed_devices_replaces_only_the_virtualhere_allowlist() -> None:
    config = """NetworkInterface=192.168.1.225
TCPPort=7575
AllowedDevices=10c4/ea60,084f/c050
AutoAttachToKernel=1
"""

    rendered = render_allowed_devices(config, ("084f/c050",))

    assert rendered == """NetworkInterface=192.168.1.225
TCPPort=7575
AllowedDevices=084f/c050
AutoAttachToKernel=1
"""


def test_plan_rejects_selection_for_a_device_missing_from_current_inventory() -> None:
    ecom = device("usb:084f:c050:-:3-1", "084f", "c050")

    with pytest.raises(RoutingPolicyError, match="not present"):
        plan_virtualhere_allowlist([ecom], {"usb:dead:beef:-:9-9": "virtualhere"})
