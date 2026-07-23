from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Callable, Mapping


class RoutingSocketError(RuntimeError):
    pass


class RoutingSocketClient:
    def __init__(self, socket_path: Path, timeout_s: float = 5.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout_s = timeout_s

    def request(self, payload: Mapping[str, object]) -> dict[str, object]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_s)
            connection.connect(str(self.socket_path))
            connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
            response = _read_line(connection)
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError as exc:
            raise RoutingSocketError("routing helper returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RoutingSocketError("routing helper returned an invalid response")
        if "error" in decoded:
            raise RoutingSocketError(str(decoded["error"]))
        return decoded


class RoutingSocketServer:
    def __init__(self, socket_path: Path, handler: Callable[[dict[str, object]], dict[str, object]]) -> None:
        self.socket_path = Path(socket_path)
        self.handler = handler
        self._listener: socket.socket | None = None
        self._unlink_on_close = True

    @classmethod
    def from_listener(
        cls,
        listener: socket.socket,
        handler: Callable[[dict[str, object]], dict[str, object]],
    ) -> "RoutingSocketServer":
        """Use a systemd-created Unix socket without taking ownership of its path."""
        socket_name = listener.getsockname()
        if not isinstance(socket_name, str):
            raise RoutingSocketError("routing helper requires a filesystem Unix socket")
        server = cls(Path(socket_name), handler)
        server._listener = listener
        server._unlink_on_close = False
        return server

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(8)
        self._listener = listener

    def serve_once(self) -> None:
        if self._listener is None:
            self.start()
        assert self._listener is not None
        connection, _ = self._listener.accept()
        with connection:
            try:
                payload = json.loads(_read_line(connection))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                response = self.handler(payload)
            except Exception as exc:
                response = {"error": str(exc)}
            connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        while True:
            self.serve_once()

    def close(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._unlink_on_close:
            self.socket_path.unlink(missing_ok=True)


def _read_line(connection: socket.socket, limit: int = 64 * 1024) -> str:
    data = bytearray()
    while len(data) < limit:
        block = connection.recv(4096)
        if not block:
            break
        data.extend(block)
        if b"\n" in block:
            break
    if not data or len(data) >= limit:
        raise RoutingSocketError("invalid routing helper message")
    return bytes(data).split(b"\n", 1)[0].decode("utf-8")
