from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

VALID_ROUTES = frozenset({"local", "virtualhere"})


class RoutingPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class VirtualHereRoutingPlan:
    allowed_devices: tuple[str, ...]
    routes: dict[str, str]


def _device_id(device: Mapping[str, str | None]) -> str:
    vendor_id = device.get("vendor_id")
    product_id = device.get("product_id")
    if not vendor_id or not product_id:
        raise RoutingPolicyError("USB inventory contains a device without VID:PID identity")
    return f"{vendor_id.lower()}/{product_id.lower()}"


def render_allowed_devices(config: str, allowed_devices: Sequence[str]) -> str:
    """Replace exactly one VirtualHere AllowedDevices setting without touching others."""
    normalized = ",".join(sorted(set(allowed_devices)))
    lines = config.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n").startswith("AllowedDevices=")]
    if len(matches) != 1:
        raise RoutingPolicyError("VirtualHere config must contain exactly one AllowedDevices setting")
    index = matches[0]
    ending = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"AllowedDevices={normalized}{ending}"
    return "".join(lines)


def plan_virtualhere_allowlist(
    devices: Sequence[Mapping[str, str | None]],
    selections: Mapping[str, str],
) -> VirtualHereRoutingPlan:
    """Create a safe VID:PID VirtualHere allowlist from physical-device selections.

    VirtualHere’s proven config interface exports by VID:PID, so selecting one
    of multiple identical attached devices is deliberately rejected.
    """
    inventory = {device.get("key"): device for device in devices}
    if None in inventory or "" in inventory:
        raise RoutingPolicyError("USB inventory contains a device without a stable key")
    unknown = sorted(set(selections) - set(inventory))
    if unknown:
        raise RoutingPolicyError(f"selected USB device is not present: {unknown[0]}")
    invalid = sorted({route for route in selections.values() if route not in VALID_ROUTES})
    if invalid:
        raise RoutingPolicyError(f"unsupported USB route: {invalid[0]}")

    routes = {key: selections.get(key, "local") for key in inventory}
    selected_ids = {_device_id(inventory[key]) for key, route in routes.items() if route == "virtualhere"}

    for device_id in selected_ids:
        matching_keys = [key for key, device in inventory.items() if _device_id(device) == device_id]
        if len(matching_keys) != 1:
            raise RoutingPolicyError(
                f"VirtualHere VID:PID allowlisting cannot select one physical device from {device_id}"
            )

    return VirtualHereRoutingPlan(
        allowed_devices=tuple(sorted(selected_ids)),
        routes=routes,
    )
