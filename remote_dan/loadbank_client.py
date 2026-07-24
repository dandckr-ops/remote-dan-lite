from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID


CollectorOwner = Literal["rdl", "windows", "off"]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_DIRECT_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


class LoadBankClient(Protocol):
    def status(self) -> dict[str, object]: ...
    def discover(self) -> dict[str, object]: ...
    def set_ownership(
        self, owner: CollectorOwner, *, confirmed_external_stopped: bool
    ) -> dict[str, object]: ...
    def start_session(
        self, candidate_id: str, duration_minutes: int, metadata: dict[str, str]
    ) -> dict[str, object]: ...
    def stop_session(self) -> dict[str, object]: ...
    def download_session(self, session_uuid: UUID) -> bytes: ...


class CollectorHttpError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CollectorUnavailableError(RuntimeError):
    pass


class BaslerCollectorClient:
    """Small server-side-only client for the loopback Basler collector API."""

    def __init__(
        self,
        base_url: str,
        *,
        password: str,
        timeout_s: float = 2.0,
        opener: Callable[..., object] | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("collector URL must be an uncredentialed loopback HTTP URL")
        if not password:
            raise ValueError("collector password must not be empty")
        if not 0.1 <= float(timeout_s) <= 10.0:
            raise ValueError("collector timeout must be between 0.1 and 10 seconds")
        self.base_url = base_url.rstrip("/")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.timeout_s = float(timeout_s)
        self._password = password
        self._opener = opener or _DIRECT_OPENER.open

    def __repr__(self) -> str:
        return f"BaslerCollectorClient(base_url={self.base_url!r}, timeout_s={self.timeout_s!r})"

    def status(self) -> dict[str, object]:
        return self._json_request("GET", "/api/status")

    def discover(self) -> dict[str, object]:
        return self._json_request("POST", "/api/discovery", {})

    def set_ownership(
        self,
        owner: CollectorOwner,
        *,
        confirmed_external_stopped: bool,
    ) -> dict[str, object]:
        return self._json_request(
            "PUT",
            "/api/ownership",
            {
                "owner": owner,
                "confirmed_external_stopped": confirmed_external_stopped,
            },
        )

    def start_session(
        self,
        candidate_id: str,
        duration_minutes: int,
        metadata: dict[str, str],
    ) -> dict[str, object]:
        return self._json_request(
            "POST",
            "/api/sessions",
            {
                "candidate_id": candidate_id,
                "duration_minutes": duration_minutes,
                "metadata": metadata,
            },
        )

    def stop_session(self) -> dict[str, object]:
        return self._json_request("POST", "/api/sessions/active/stop", {})

    def download_session(self, session_uuid: UUID) -> bytes:
        return self._request(
            "GET",
            f"/api/sessions/{session_uuid}/download",
            expect_json=False,
        )

    def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        result = self._request(method, path, payload, expect_json=True)
        if not isinstance(result, dict):
            raise CollectorHttpError(502, "collector returned a non-object JSON response")
        return result

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        expect_json: bool,
    ) -> dict[str, object] | bytes:
        token = base64.b64encode(f"operator:{self._password}".encode("utf-8")).decode("ascii")
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json" if expect_json else "application/zip",
                **(
                    {"Origin": self.origin}
                    if method in {"POST", "PUT", "PATCH", "DELETE"}
                    else {}
                ),
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        except HTTPError as exc:
            detail = self._http_error_detail(exc)
            raise CollectorHttpError(exc.code, detail) from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CollectorUnavailableError(f"collector unavailable: {reason}") from exc

        if not expect_json:
            if content_type != "application/zip":
                raise CollectorHttpError(502, "collector returned a non-ZIP download response")
            return body
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CollectorHttpError(502, "collector returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise CollectorHttpError(502, "collector returned a non-object JSON response")
        return decoded

    @staticmethod
    def _http_error_detail(exc: HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return f"collector returned HTTP {exc.code} {exc.reason}"
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
            return payload["detail"]
        return f"collector returned HTTP {exc.code} {exc.reason}"


def load_client_from_environment(
    environment: Mapping[str, str] | None = None,
) -> BaslerCollectorClient | None:
    env = os.environ if environment is None else environment
    base_url = env.get("REMOTE_DAN_LOADBANK_URL")
    password_file = env.get("REMOTE_DAN_LOADBANK_PASSWORD_FILE")
    if not base_url or not password_file:
        return None
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
        if not password:
            return None
        timeout_s = float(env.get("REMOTE_DAN_LOADBANK_TIMEOUT_SECONDS", "2.0"))
        return BaslerCollectorClient(base_url, password=password, timeout_s=timeout_s)
    except (OSError, UnicodeError, ValueError):
        return None
