from __future__ import annotations

from datetime import UTC, datetime
import threading
from typing import Any, Callable

from remote_dan.database import EvidenceDatabase
from remote_dan.obd_protocol import (
    PID_DEFINITIONS,
    OBDProtocolError,
    decode_dtc_payload,
    decode_live_pid,
    decode_readiness,
    decode_supported_pids,
    decode_vin,
    parse_elm_response,
)
from remote_dan.obd_provider import (
    ELMSerialProvider,
    OBDInUse,
    OBDNotConnected,
    OBDProvider,
    SimulatorOBDProvider,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class OBDService:
    """Single serialized owner for generic OBD request/response operations."""

    def __init__(
        self,
        *,
        database: EvidenceDatabase,
        simulator_provider: OBDProvider | None = None,
        hardware_provider_factory: Callable[[], OBDProvider] | None = None,
    ) -> None:
        self.database = database
        self.simulator_provider = simulator_provider or SimulatorOBDProvider()
        self.hardware_provider_factory = hardware_provider_factory or ELMSerialProvider
        self._lock = threading.RLock()
        self._provider: OBDProvider | None = None
        self._identity: dict[str, Any] | None = None
        self._supported_pids: set[int] = set()
        self._connection_id: int | None = None
        self._session_id: int | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._provider is None or self._identity is None:
                return {
                    "connected": False,
                    "provider": None,
                    "adapter_identity": None,
                    "stable_path": None,
                    "protocol": None,
                    "responder_ids": [],
                    "voltage": None,
                    "supported_pids": [],
                    "connection_generation": None,
                    "connection_id": None,
                    "session_id": None,
                    "hardware_clear_enabled": False,
                }
            return {
                "connected": True,
                **self._identity,
                "supported_pids": [f"{pid:02X}" for pid in sorted(self._supported_pids)],
                "connection_id": self._connection_id,
                "session_id": self._session_id,
                "hardware_clear_enabled": False,
            }

    def connect(self, *, mode: str, session_id: int | None) -> dict[str, Any]:
        with self._lock:
            if self._provider is not None:
                raise OBDInUse("an OBD provider is already connected")
            if mode == "simulator":
                provider = self.simulator_provider
            elif mode == "hardware":
                provider = self.hardware_provider_factory()
            else:
                raise ValueError("OBD mode must be simulator or hardware")
            try:
                identity = provider.connect()
                supported, responders = self._discover_supported_pids(provider)
                identity["responder_ids"] = sorted(
                    set(identity.get("responder_ids", [])) | responders
                )
                connection_id = self.database.create_obd_connection(
                    session_id=session_id,
                    provider=str(identity["provider"]),
                    adapter_identity=str(identity["adapter_identity"]),
                    stable_path=(
                        str(identity["stable_path"])
                        if identity.get("stable_path") is not None
                        else None
                    ),
                    protocol=str(identity["protocol"]),
                    responder_ids=list(identity["responder_ids"]),
                    voltage=(
                        float(identity["voltage"])
                        if identity.get("voltage") is not None
                        else None
                    ),
                )
            except Exception:
                provider.disconnect()
                raise
            self._provider = provider
            self._identity = identity
            self._supported_pids = supported
            self._connection_id = connection_id
            self._session_id = session_id
            return self.status()

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            provider = self._provider
            connection_id = self._connection_id
            self._provider = None
            self._identity = None
            self._supported_pids = set()
            self._connection_id = None
            self._session_id = None
            if provider is not None:
                provider.disconnect()
            if connection_id is not None:
                self.database.close_obd_connection(connection_id, status="closed")
            return self.status()

    def _require_provider(self) -> OBDProvider:
        if self._provider is None:
            raise OBDNotConnected("OBD is not connected")
        return self._provider

    def _discover_supported_pids(
        self,
        provider: OBDProvider,
    ) -> tuple[set[int], set[str]]:
        supported: set[int] = set()
        responders: set[str] = set()
        page = 0x00
        while page <= 0xA0:
            payloads = parse_elm_response(provider.query(f"01{page:02X}"))
            if not payloads:
                if page == 0:
                    raise OBDProtocolError("vehicle returned no supported-PID bitmap")
                break
            responders.update(payloads)
            page_pids: set[int] = set()
            for payload in payloads.values():
                if len(payload) >= 2 and payload[:2] == bytes((0x41, page)):
                    page_pids.update(decode_supported_pids(payload))
            if not page_pids and page == 0:
                raise OBDProtocolError("vehicle returned no valid supported-PID bitmap")
            supported.update(page_pids)
            next_page = page + 0x20
            if next_page not in page_pids:
                break
            page = next_page
        return supported, responders

    def read_live(self) -> dict[str, Any]:
        with self._lock:
            provider = self._require_provider()
            sampled_at = _utc_now()
            values: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            raw_responses: dict[str, str] = {}
            for pid in sorted(self._supported_pids & PID_DEFINITIONS.keys()):
                command = f"01{pid:02X}"
                try:
                    raw = provider.query(command)
                    raw_responses[command] = raw
                    payloads = parse_elm_response(raw)
                    if not payloads:
                        errors.append({"command": command, "error": "no_data"})
                        continue
                    for ecu, payload in payloads.items():
                        values.append(
                            decode_live_pid(payload, ecu=ecu, sampled_at=sampled_at)
                        )
                except (OBDProtocolError, RuntimeError, ValueError) as exc:
                    errors.append({"command": command, "error": str(exc)})
            return {
                "sampled_at": sampled_at,
                "values": values,
                "errors": errors,
                "raw_responses": raw_responses,
            }

    def read_faults(self) -> dict[str, Any]:
        with self._lock:
            provider = self._require_provider()
            observed_at = _utc_now()
            raw_responses: dict[str, str] = {}
            readiness: list[dict[str, Any]] = []
            raw = provider.query("0101")
            raw_responses["0101"] = raw
            for ecu, payload in parse_elm_response(raw).items():
                readiness.append(decode_readiness(payload, ecu=ecu))

            grouped: dict[str, list[dict[str, Any]]] = {
                "stored": [], "pending": [], "permanent": [],
            }
            status: dict[str, str] = {}
            for command, state in (("03", "stored"), ("07", "pending"), ("0A", "permanent")):
                raw = provider.query(command)
                raw_responses[command] = raw
                payloads = parse_elm_response(raw)
                if not payloads:
                    status[state] = "no_data"
                    continue
                status[state] = "complete"
                for ecu, payload in payloads.items():
                    grouped[state].extend(
                        decode_dtc_payload(payload, state=state, ecu=ecu)  # type: ignore[arg-type]
                    )
            return {
                "observed_at": observed_at,
                "readiness": readiness,
                **grouped,
                "stored_status": status.get("stored", "no_data"),
                "pending_status": status.get("pending", "no_data"),
                "permanent_status": status.get("permanent", "no_data"),
                "raw_responses": raw_responses,
            }

    def read_vehicle_info(self) -> dict[str, Any]:
        with self._lock:
            provider = self._require_provider()
            if self._identity is None:
                raise OBDNotConnected("OBD identity is unavailable")
            raw = provider.query("0902")
            payloads = parse_elm_response(raw)
            vins = [
                {"ecu": ecu, "vin": decode_vin(payload)}
                for ecu, payload in sorted(payloads.items())
            ]
            distinct = {item["vin"] for item in vins}
            return {
                "observed_at": _utc_now(),
                "vins": vins,
                "vin_mismatch": len(distinct) > 1,
                "provider": self._identity["provider"],
                "adapter_identity": self._identity["adapter_identity"],
                "protocol": self._identity["protocol"],
                "responder_ids": list(self._identity.get("responder_ids", [])),
                "voltage": self._identity.get("voltage"),
                "raw_responses": {"0902": raw},
            }

    def clear_faults(self) -> None:
        raise PermissionError(
            "hardware fault clearing requires an authenticated operator identity"
        )
