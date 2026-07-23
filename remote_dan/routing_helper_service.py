from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Mapping, Sequence

from remote_dan.routing_socket import RoutingSocketServer
from remote_dan.virtualhere_helper import (
    RoutingHelper,
    RoutingHelperError,
    _allowed_devices_from_config,
    helper_request_handler,
)

CONFIG_PATH = Path("/etc/virtualhere/config.ini")
AUDIT_PATH = Path("/var/lib/remote-dan-routing/routing-audit.jsonl")
VIRTUALHERE_SERVICE = "virtualhere-basler.service"
SYSTEMD_LISTEN_FD = 3


def systemctl_restart() -> None:
    subprocess.run(["systemctl", "restart", VIRTUALHERE_SERVICE], check=True)


def systemctl_is_active() -> bool:
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", VIRTUALHERE_SERVICE], check=False
    ).returncode == 0


def build_root_helper(config_path: Path = CONFIG_PATH) -> RoutingHelper:
    """Build the only privileged routing authority used by the socket service."""
    return RoutingHelper(
        config_path=config_path,
        restart=systemctl_restart,
        is_active=systemctl_is_active,
    )


class JsonlAuditLog:
    """Append and fsync each routing outcome before acknowledging it to the caller."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                offset += os.write(fd, encoded[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)


class RoutingHelperService:
    """Socket request adapter limited to status/apply with durable apply audit records."""

    def __init__(self, helper: RoutingHelper, *, audit_path: Path = AUDIT_PATH) -> None:
        self.helper = helper
        self.audit_path = Path(audit_path)
        self.audit = JsonlAuditLog(self.audit_path)
        self._handle_helper_request = helper_request_handler(helper)

    def handle(self, request: dict[str, object]) -> dict[str, object]:
        action = request.get("action")
        if action == "status":
            return self._handle_helper_request(request)
        if action != "apply":
            raise RoutingHelperError("unsupported routing action")

        old_allowed_devices = _allowed_devices_from_config(self.helper.config_path)
        try:
            response = self._handle_helper_request(request)
        except Exception as exc:
            self._append_apply_audit(
                old_allowed_devices=old_allowed_devices,
                new_allowed_devices=_allowed_devices_from_config(self.helper.config_path),
                outcome="error",
                error=str(exc),
            )
            raise

        new_allowed_devices = response.get("allowed_devices")
        if not isinstance(new_allowed_devices, list) or not all(
            isinstance(value, str) for value in new_allowed_devices
        ):
            raise RoutingHelperError("routing helper returned invalid allowed devices")
        self._append_apply_audit(
            old_allowed_devices=old_allowed_devices,
            new_allowed_devices=new_allowed_devices,
            outcome="success",
        )
        return response

    def _append_apply_audit(
        self,
        *,
        old_allowed_devices: Sequence[str],
        new_allowed_devices: Sequence[str],
        outcome: str,
        error: str | None = None,
    ) -> None:
        record: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": "apply",
            "old_allowed_devices": list(old_allowed_devices),
            "new_allowed_devices": list(new_allowed_devices),
            "outcome": outcome,
        }
        if error is not None:
            record["error"] = error
        self.audit.append(record)


def serve_socket_activated(service: RoutingHelperService, *, listen_fd: int = SYSTEMD_LISTEN_FD) -> None:
    """Serve the inherited systemd Unix socket; this process never creates HTTP listeners."""
    listener = socket.fromfd(listen_fd, socket.AF_UNIX, socket.SOCK_STREAM)
    server = RoutingSocketServer.from_listener(listener, handler=service.handle)
    try:
        server.serve_forever()
    finally:
        server.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remote Dan Lite root USB-routing socket helper")
    parser.add_argument(
        "--socket-activation",
        action="store_true",
        help="serve only the inherited systemd Unix socket",
    )
    arguments = parser.parse_args(argv)
    if not arguments.socket_activation:
        parser.error("--socket-activation is required; this helper has no standalone listener")
    return arguments


def main(argv: Sequence[str] | None = None) -> None:
    parse_args(argv)
    serve_socket_activated(RoutingHelperService(build_root_helper()))


if __name__ == "__main__":
    main()
