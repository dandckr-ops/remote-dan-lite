from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

from remote_dan.usb_inventory import list_usb_devices
from remote_dan.usb_routing import RoutingPolicyError, plan_virtualhere_allowlist
from remote_dan.virtualhere_admin import apply_allowed_devices


class RoutingHelperError(RuntimeError):
    pass


def inventory_revision(devices: Sequence[Mapping[str, str | None]]) -> str:
    """Fingerprint stable USB identity, never volatile /dev/ttyUSB numbering."""
    identities = sorted(
        (
            device.get("key"),
            device.get("vendor_id"),
            device.get("product_id"),
            device.get("serial"),
            device.get("topology_path"),
        )
        for device in devices
    )
    encoded = json.dumps(identities, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RoutingHelper:
    """Root-side VirtualHere authority with a deliberately narrow apply surface."""

    def __init__(
        self,
        *,
        config_path: Path,
        inventory_probe: Callable[[], list[dict[str, str | None]]] = list_usb_devices,
        restart: Callable[[], None],
        is_active: Callable[[], bool],
    ) -> None:
        self.config_path = Path(config_path)
        self.inventory_probe = inventory_probe
        self.restart = restart
        self.is_active = is_active

    def apply(
        self,
        *,
        expected_inventory_revision: str,
        routes: Mapping[str, str],
    ) -> dict[str, object]:
        devices = self.inventory_probe()
        actual_revision = inventory_revision(devices)
        if actual_revision != expected_inventory_revision:
            raise RoutingHelperError("USB inventory changed; refresh the HMI and confirm the routes again")
        try:
            plan = plan_virtualhere_allowlist(devices, routes)
        except RoutingPolicyError as exc:
            raise RoutingHelperError(str(exc)) from exc
        try:
            apply_allowed_devices(
                self.config_path,
                plan.allowed_devices,
                restart=self.restart,
                is_active=self.is_active,
            )
        except Exception as exc:
            raise RoutingHelperError(str(exc)) from exc
        return {
            "inventory_revision": actual_revision,
            "allowed_devices": list(plan.allowed_devices),
            "routes": plan.routes,
        }


def _allowed_devices_from_config(config_path: Path) -> list[str]:
    for line in config_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("AllowedDevices="):
            return [value for value in line.partition("=")[2].split(",") if value]
    raise RoutingHelperError("VirtualHere config has no AllowedDevices setting")


def helper_request_handler(helper: RoutingHelper) -> Callable[[dict[str, object]], dict[str, object]]:
    def handle(request: dict[str, object]) -> dict[str, object]:
        action = request.get("action")
        if action == "status":
            devices = helper.inventory_probe()
            return {
                "available": True,
                "inventory_revision": inventory_revision(devices),
                "allowed_devices": _allowed_devices_from_config(helper.config_path),
            }
        if action != "apply":
            raise RoutingHelperError("unsupported routing action")
        revision = request.get("inventory_revision")
        routes = request.get("routes")
        if not isinstance(revision, str) or not isinstance(routes, dict):
            raise RoutingHelperError("routing apply request is malformed")
        return helper.apply(expected_inventory_revision=revision, routes=routes)
    return handle
