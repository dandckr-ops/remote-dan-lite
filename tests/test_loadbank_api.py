from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from remote_dan.app import create_app
from remote_dan.loadbank_client import CollectorHttpError, CollectorUnavailableError


SESSION_UUID = UUID("4b74e7d4-d334-470f-9eb5-9ba00f2d05ac")


class FakeLoadBankClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def status(self) -> dict[str, object]:
        self.calls.append(("status",))
        return {
            "active_session": {
                "id": str(SESSION_UUID),
                "status": "active",
                "captured_snapshots": 4,
                "expected_snapshots": 8,
            },
            "latest_snapshot": {"quality": "good"},
            "recent_sessions": [{"id": str(SESSION_UUID), "status": "active"}],
            "ownership": {"owner": "rdl"},
        }

    def discover(self) -> dict[str, object]:
        self.calls.append(("discover",))
        return {"controllers": [{"candidate_id": "basler-direct", "label": "Basler DECS-250"}]}

    def set_ownership(self, owner: str, *, confirmed_external_stopped: bool) -> dict[str, object]:
        self.calls.append(("set_ownership", owner, confirmed_external_stopped))
        return {"owner": owner}

    def start_session(
        self,
        candidate_id: str,
        duration_minutes: int,
        metadata: dict[str, str],
    ) -> dict[str, object]:
        self.calls.append(("start_session", candidate_id, duration_minutes, metadata))
        return {
            "session": {"id": str(SESSION_UUID), "status": "active"},
            "initial_collection": {"status": "collected", "ordinal": 1},
        }

    def stop_session(self) -> dict[str, object]:
        self.calls.append(("stop_session",))
        return {"stopped_session": {"id": str(SESSION_UUID), "status": "stopped"}}

    def download_session(self, session_uuid: UUID) -> bytes:
        self.calls.append(("download_session", session_uuid))
        return b"PK\x03\x04collector-evidence"


def app_client(tmp_path: Path, collector: object | None) -> TestClient:
    return TestClient(
        create_app(
            data_dir=tmp_path,
            loadbank_client=collector,
            loadbank_allowed_origins={"http://testserver"},
        ),
        headers={"Origin": "http://testserver"},
    )


def test_loadbank_mutations_require_allowed_origin_and_json(tmp_path: Path) -> None:
    collector = FakeLoadBankClient()
    app = create_app(
        data_dir=tmp_path,
        loadbank_client=collector,
        loadbank_allowed_origins={"http://testserver"},
    )
    client = TestClient(app)

    status = client.get(
        "/api/loadbank/status",
        headers={"Origin": "http://evil.example"},
    )
    evil_origin = client.post(
        "/api/loadbank/discovery",
        json={},
        headers={"Origin": "http://evil.example"},
    )
    missing_origin = client.post("/api/loadbank/discovery", json={})
    form_encoded = client.post(
        "/api/loadbank/discovery",
        data={},
        headers={"Origin": "http://testserver"},
    )
    allowed = client.post(
        "/api/loadbank/discovery",
        json={},
        headers={"Origin": "http://testserver"},
    )

    assert status.status_code == 200
    assert evil_origin.status_code == 403
    assert missing_origin.status_code == 403
    assert form_encoded.status_code == 415
    assert allowed.status_code == 200
    assert collector.calls == [("status",), ("discover",)]


def test_loadbank_proxy_exposes_frozen_status_and_discovery_contract(tmp_path: Path) -> None:
    collector = FakeLoadBankClient()
    client = app_client(tmp_path, collector)

    status = client.get("/api/loadbank/status")
    discovery = client.post("/api/loadbank/discovery", json={})

    assert status.status_code == 200
    assert status.json()["ownership"] == {"owner": "rdl"}
    assert discovery.status_code == 200
    assert discovery.json()["controllers"][0]["candidate_id"] == "basler-direct"
    assert collector.calls == [("status",), ("discover",)]


def test_loadbank_bodyless_actions_reject_browser_supplied_fields(tmp_path: Path) -> None:
    collector = FakeLoadBankClient()
    client = app_client(tmp_path, collector)

    discovery = client.post(
        "/api/loadbank/discovery",
        json={"upstream_url": "http://192.168.1.99:9999"},
    )
    stopped = client.post(
        "/api/loadbank/sessions/active/stop",
        json={"force": True},
    )

    assert discovery.status_code == 422
    assert stopped.status_code == 422
    assert collector.calls == []


def test_loadbank_proxy_forwards_strict_ownership_and_session_requests(tmp_path: Path) -> None:
    collector = FakeLoadBankClient()
    client = app_client(tmp_path, collector)
    metadata = {
        "customer": "Acme Power",
        "work_order": "WO-42",
        "generator": "GEN-1",
        "technician": "Daniel",
    }

    ownership = client.put("/api/loadbank/ownership", json={
        "owner": "rdl",
        "confirmed_external_stopped": True,
    })
    started = client.post("/api/loadbank/sessions", json={
        "candidate_id": "basler-direct",
        "duration_minutes": 60,
        "metadata": metadata,
    })
    stopped = client.post("/api/loadbank/sessions/active/stop", json={})

    assert ownership.status_code == 200
    assert ownership.json() == {"owner": "rdl"}
    assert started.status_code == 201
    assert started.json()["initial_collection"] == {"status": "collected", "ordinal": 1}
    assert stopped.status_code == 200
    assert collector.calls == [
        ("set_ownership", "rdl", True),
        ("start_session", "basler-direct", 60, metadata),
        ("stop_session",),
    ]


@pytest.mark.parametrize("duration", [0, 14, 16, 1441, "60", 60.0, True])
def test_loadbank_session_duration_is_strict_bounded_and_in_fifteen_minute_steps(
    tmp_path: Path,
    duration: object,
) -> None:
    collector = FakeLoadBankClient()
    response = app_client(tmp_path, collector).post("/api/loadbank/sessions", json={
        "candidate_id": "basler-direct",
        "duration_minutes": duration,
        "metadata": {
            "customer": "Acme",
            "work_order": "WO-42",
            "generator": "GEN-1",
            "technician": "Daniel",
        },
    })

    assert response.status_code == 422
    assert collector.calls == []


@pytest.mark.parametrize("payload", [
    {"owner": "rdl", "confirmed_external_stopped": "true"},
    {"owner": "other", "confirmed_external_stopped": False},
    {"owner": "off", "confirmed_external_stopped": False, "upstream_url": "http://evil"},
])
def test_loadbank_ownership_schema_rejects_coercion_unknown_owner_and_extra_fields(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    collector = FakeLoadBankClient()

    response = app_client(tmp_path, collector).put("/api/loadbank/ownership", json=payload)

    assert response.status_code == 422
    assert collector.calls == []


def test_loadbank_session_metadata_is_named_bounded_and_forbids_extras(tmp_path: Path) -> None:
    collector = FakeLoadBankClient()
    client = app_client(tmp_path, collector)
    base = {
        "candidate_id": "basler-direct",
        "duration_minutes": 60,
        "metadata": {
            "customer": "Acme",
            "work_order": "WO-42",
            "generator": "GEN-1",
            "technician": "Daniel",
        },
    }

    missing = client.post("/api/loadbank/sessions", json={
        **base,
        "metadata": {key: value for key, value in base["metadata"].items() if key != "work_order"},
    })
    extra = client.post("/api/loadbank/sessions", json={
        **base,
        "metadata": {**base["metadata"], "notes": "not in frozen metadata"},
    })
    arbitrary_url = client.post("/api/loadbank/sessions", json={
        **base,
        "upstream_url": "http://192.168.1.99:9999",
    })

    assert [missing.status_code, extra.status_code, arbitrary_url.status_code] == [422, 422, 422]
    assert collector.calls == []


def test_loadbank_proxy_preserves_conflict_and_maps_unreachable_to_503(tmp_path: Path) -> None:
    class ConflictCollector(FakeLoadBankClient):
        def set_ownership(self, owner: str, *, confirmed_external_stopped: bool) -> dict[str, object]:
            raise CollectorHttpError(409, "Windows collector is still active")

    class UnreachableCollector(FakeLoadBankClient):
        def status(self) -> dict[str, object]:
            raise CollectorUnavailableError("collector unavailable: connection refused")

    conflict = app_client(tmp_path / "conflict", ConflictCollector()).put(
        "/api/loadbank/ownership",
        json={"owner": "rdl", "confirmed_external_stopped": True},
    )
    unavailable = app_client(tmp_path / "unavailable", UnreachableCollector()).get(
        "/api/loadbank/status"
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Windows collector is still active"
    assert unavailable.status_code == 503
    assert "connection refused" in unavailable.json()["detail"]


def test_loadbank_is_unavailable_without_server_side_configuration_but_hmi_starts(
    tmp_path: Path,
) -> None:
    client = app_client(tmp_path, None)

    status = client.get("/api/loadbank/status")
    index = client.get("/")

    assert status.status_code == 503
    assert status.json()["detail"] == "Load Bank unavailable: collector is not configured"
    assert index.status_code == 200
    assert "Remote Dan Lite" in index.text


def test_loadbank_download_is_uuid_bounded_and_returned_as_zip(tmp_path: Path) -> None:
    collector = FakeLoadBankClient()
    client = app_client(tmp_path, collector)

    downloaded = client.get(f"/api/loadbank/sessions/{SESSION_UUID}/download")
    invalid = client.get("/api/loadbank/sessions/not-a-uuid/download")

    assert downloaded.status_code == 200
    assert downloaded.content == b"PK\x03\x04collector-evidence"
    assert downloaded.headers["content-type"] == "application/zip"
    assert downloaded.headers["content-disposition"] == (
        f'attachment; filename="load-bank-{SESSION_UUID}.zip"'
    )
    assert invalid.status_code == 422
    assert collector.calls == [("download_session", SESSION_UUID)]
