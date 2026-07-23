from __future__ import annotations

import threading
from pathlib import Path
import socket

from remote_dan.routing_socket import RoutingSocketClient, RoutingSocketServer


def test_unix_socket_client_returns_helper_response(tmp_path: Path) -> None:
    socket_path = tmp_path / "routing.sock"
    server = RoutingSocketServer(socket_path, handler=lambda request: {"ok": True, "action": request["action"]})
    server.start()
    thread = threading.Thread(target=server.serve_once, daemon=True)
    thread.start()

    response = RoutingSocketClient(socket_path).request({"action": "status"})

    thread.join(timeout=1)
    server.close()
    assert response == {"ok": True, "action": "status"}


def test_inherited_listener_is_not_unlinked_when_server_closes(tmp_path: Path) -> None:
    socket_path = tmp_path / "systemd-created.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    server = RoutingSocketServer.from_listener(listener, handler=lambda request: {"ok": True})

    server.close()

    assert socket_path.exists()
    socket_path.unlink()
