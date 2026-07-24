from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import socket
from threading import Thread
import urllib.request
from urllib.error import HTTPError, URLError
from uuid import UUID

import pytest

from remote_dan.loadbank_client import (
    BaslerCollectorClient,
    CollectorHttpError,
    CollectorUnavailableError,
    load_client_from_environment,
)


class FakeResponse:
    def __init__(self, payload: object, *, content_type: str = "application/json") -> None:
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class RecordingOpener:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request: object, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_client_uses_fixed_loopback_url_basic_auth_json_and_bounded_timeout() -> None:
    opener = RecordingOpener([FakeResponse({"ownership": {"owner": "rdl"}})])
    client = BaslerCollectorClient(
        "http://127.0.0.1:8788",
        password="server-only-secret",
        timeout_s=2.5,
        opener=opener,
    )

    result = client.status()

    request, timeout = opener.calls[0]
    assert result == {"ownership": {"owner": "rdl"}}
    assert request.full_url == "http://127.0.0.1:8788/api/status"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Basic b3BlcmF0b3I6c2VydmVyLW9ubHktc2VjcmV0"
    assert request.get_header("Origin") is None
    assert request.data is None
    assert timeout == 2.5


def test_default_transport_ignores_hostile_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destinations: list[tuple[str, int]] = []

    def record_destination(
        address: tuple[str, int],
        *args: object,
        **kwargs: object,
    ) -> object:
        destinations.append(address)
        raise OSError("bounded destination probe")

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:6553")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:6553")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(socket, "create_connection", record_destination)
    client = BaslerCollectorClient(
        "http://127.0.0.1:8788",
        password="secret",
        timeout_s=0.1,
    )

    with pytest.raises(CollectorUnavailableError, match="bounded destination probe"):
        client.status()

    assert destinations == [("127.0.0.1", 8788)]


def test_default_transport_rejects_redirected_authority_response() -> None:
    class RedirectingHandler(BaseHTTPRequestHandler):
        followed_redirect = False

        def do_GET(self) -> None:
            if self.path == "/api/status":
                self.send_response(302)
                self.send_header("Location", "/impersonated-status")
                self.end_headers()
                return
            type(self).followed_redirect = True
            payload = b'{"ownership":{"owner":"rdl"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = BaslerCollectorClient(
            f"http://127.0.0.1:{server.server_port}",
            password="secret",
            timeout_s=1.0,
        )

        with pytest.raises(CollectorHttpError) as raised:
            client.status()

        assert raised.value.status_code == 302
        assert not RedirectingHandler.followed_redirect
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_client_maps_each_operation_to_frozen_collector_contract() -> None:
    opener = RecordingOpener([
        FakeResponse({"controllers": [{"candidate_id": "direct-basler"}]}),
        FakeResponse({"owner": "windows"}),
        FakeResponse({"session": {"uuid": "4b74e7d4-d334-470f-9eb5-9ba00f2d05ac"}}),
        FakeResponse({"session": {"state": "stopped"}}),
        FakeResponse(b"PK\x03\x04evidence", content_type="application/zip"),
    ])
    client = BaslerCollectorClient(
        "http://localhost:8788/",
        password="secret",
        opener=opener,
    )
    metadata = {
        "customer": "Acme",
        "work_order": "WO-42",
        "generator": "GEN-1",
        "technician": "Daniel",
    }

    assert client.discover()["controllers"][0]["candidate_id"] == "direct-basler"
    assert client.set_ownership("windows", confirmed_external_stopped=False)["owner"] == "windows"
    assert client.start_session("direct-basler", 60, metadata)["session"]["uuid"].startswith("4b74")
    assert client.stop_session()["session"]["state"] == "stopped"
    assert client.download_session(UUID("4b74e7d4-d334-470f-9eb5-9ba00f2d05ac")) == b"PK\x03\x04evidence"

    requests = [call[0] for call in opener.calls]
    assert [(item.get_method(), item.full_url) for item in requests] == [
        ("POST", "http://localhost:8788/api/discovery"),
        ("PUT", "http://localhost:8788/api/ownership"),
        ("POST", "http://localhost:8788/api/sessions"),
        ("POST", "http://localhost:8788/api/sessions/active/stop"),
        ("GET", "http://localhost:8788/api/sessions/4b74e7d4-d334-470f-9eb5-9ba00f2d05ac/download"),
    ]
    assert json.loads(requests[0].data) == {}
    assert json.loads(requests[1].data) == {
        "owner": "windows",
        "confirmed_external_stopped": False,
    }
    assert json.loads(requests[2].data) == {
        "candidate_id": "direct-basler",
        "duration_minutes": 60,
        "metadata": metadata,
    }
    assert json.loads(requests[3].data) == {}
    assert [request.get_header("Origin") for request in requests] == [
        "http://localhost:8788",
        "http://localhost:8788",
        "http://localhost:8788",
        "http://localhost:8788",
        None,
    ]


def test_client_preserves_upstream_http_status_and_honest_detail() -> None:
    upstream_error = HTTPError(
        "http://127.0.0.1:8788/api/ownership",
        409,
        "Conflict",
        {},
        BytesIO(b'{"detail":"Windows collector is still active"}'),
    )
    client = BaslerCollectorClient(
        "http://127.0.0.1:8788",
        password="secret",
        opener=RecordingOpener([upstream_error]),
    )

    with pytest.raises(CollectorHttpError) as raised:
        client.set_ownership("rdl", confirmed_external_stopped=True)

    assert raised.value.status_code == 409
    assert raised.value.detail == "Windows collector is still active"


def test_client_maps_unreachable_and_invalid_json_to_specific_errors() -> None:
    unreachable = BaslerCollectorClient(
        "http://127.0.0.1:8788",
        password="secret",
        opener=RecordingOpener([URLError("connection refused")]),
    )
    invalid = BaslerCollectorClient(
        "http://127.0.0.1:8788",
        password="secret",
        opener=RecordingOpener([FakeResponse(b"not-json")]),
    )

    with pytest.raises(CollectorUnavailableError, match="connection refused"):
        unreachable.status()
    with pytest.raises(CollectorHttpError) as raised:
        invalid.status()
    assert raised.value.status_code == 502
    assert "invalid JSON" in raised.value.detail


def test_client_rejects_non_zip_download_response() -> None:
    client = BaslerCollectorClient(
        "http://127.0.0.1:8788",
        password="secret",
        opener=RecordingOpener([FakeResponse({"detail": "not a ZIP"})]),
    )

    with pytest.raises(CollectorHttpError) as raised:
        client.download_session(UUID("4b74e7d4-d334-470f-9eb5-9ba00f2d05ac"))

    assert raised.value.status_code == 502
    assert "ZIP" in raised.value.detail


def test_client_rejects_non_loopback_or_credential_bearing_base_urls() -> None:
    for base_url in (
        "http://192.168.1.99:8788",
        "https://collector.example.com",
        "http://operator:secret@127.0.0.1:8788",
        "http://127.0.0.1:8788/prefix",
        "file:///run/collector.sock",
    ):
        with pytest.raises(ValueError):
            BaslerCollectorClient(base_url, password="secret")


def test_environment_factory_requires_explicit_url_and_password_file(tmp_path, monkeypatch) -> None:
    credential = tmp_path / "loadbank-password"
    credential.write_text("credential-secret\n", encoding="utf-8")

    assert load_client_from_environment({}) is None
    assert load_client_from_environment({"REMOTE_DAN_LOADBANK_URL": "http://127.0.0.1:8788"}) is None
    assert load_client_from_environment({"REMOTE_DAN_LOADBANK_PASSWORD_FILE": str(credential)}) is None

    client = load_client_from_environment({
        "REMOTE_DAN_LOADBANK_URL": "http://127.0.0.1:8788",
        "REMOTE_DAN_LOADBANK_PASSWORD_FILE": str(credential),
        "REMOTE_DAN_LOADBANK_TIMEOUT_SECONDS": "1.75",
    })

    assert client is not None
    assert client.base_url == "http://127.0.0.1:8788"
    assert client.timeout_s == 1.75
    assert "credential-secret" not in repr(client)


def test_environment_factory_fails_closed_without_breaking_startup(tmp_path) -> None:
    missing = tmp_path / "missing-password"
    blank = tmp_path / "blank-password"
    blank.write_text("\n", encoding="utf-8")

    assert load_client_from_environment({
        "REMOTE_DAN_LOADBANK_URL": "http://127.0.0.1:8788",
        "REMOTE_DAN_LOADBANK_PASSWORD_FILE": str(missing),
    }) is None
    assert load_client_from_environment({
        "REMOTE_DAN_LOADBANK_URL": "http://127.0.0.1:8788",
        "REMOTE_DAN_LOADBANK_PASSWORD_FILE": str(blank),
    }) is None
