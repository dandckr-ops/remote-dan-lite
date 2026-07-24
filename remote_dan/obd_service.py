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
    parse_elm_response_scoped,
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
            except Exception as primary_error:
                try:
                    provider.disconnect()
                except Exception as cleanup_error:
                    primary_error.add_note(
                        f"OBD provider cleanup also failed: {cleanup_error}"
                    )
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
            disconnect_error: Exception | None = None
            database_error: Exception | None = None
            try:
                if provider is not None:
                    provider.disconnect()
            except Exception as exc:
                disconnect_error = exc
            try:
                if connection_id is not None:
                    self.database.close_obd_connection(
                        connection_id,
                        status="error" if disconnect_error else "closed",
                        error=(str(disconnect_error)[:1000] if disconnect_error else None),
                    )
            except Exception as exc:
                database_error = exc
            finally:
                self._provider = None
                self._identity = None
                self._supported_pids = set()
                self._connection_id = None
                self._session_id = None
            if disconnect_error is not None and database_error is not None:
                raise ExceptionGroup(
                    "OBD provider and database disconnect cleanup failed",
                    [disconnect_error, database_error],
                )
            if disconnect_error is not None:
                raise disconnect_error
            if database_error is not None:
                raise database_error
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
            payloads, _ = parse_elm_response_scoped(provider.query(f"01{page:02X}"))
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
                raw = provider.query(command)
                raw_responses[command] = raw
                payloads, frame_errors = parse_elm_response_scoped(raw)
                errors.extend({"command": command, **item} for item in frame_errors)
                if not payloads:
                    if not frame_errors:
                        errors.append({"command": command, "error": "no_data"})
                    continue
                for ecu, payload in payloads.items():
                    try:
                        decoded = decode_live_pid(payload, ecu=ecu, sampled_at=sampled_at)
                        expected_pid = f"{pid:02X}"
                        if decoded["pid"] != expected_pid:
                            raise OBDProtocolError(
                                f"response PID {decoded['pid']} does not match requested PID {expected_pid}"
                            )
                        values.append(decoded)
                    except OBDProtocolError as exc:
                        errors.append(
                            {"command": command, "ecu": ecu, "error": str(exc)}
                        )
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
            errors: list[dict[str, str]] = []
            raw = provider.query("0101")
            raw_responses["0101"] = raw
            readiness_payloads, readiness_frame_errors = parse_elm_response_scoped(raw)
            errors.extend({"command": "0101", **item} for item in readiness_frame_errors)
            for ecu, payload in readiness_payloads.items():
                try:
                    readiness.append(decode_readiness(payload, ecu=ecu))
                except OBDProtocolError as exc:
                    errors.append({"command": "0101", "ecu": ecu, "error": str(exc)})

            grouped: dict[str, list[dict[str, Any]]] = {
                "stored": [], "pending": [], "permanent": [],
            }
            status: dict[str, str] = {}
            for command, state in (("03", "stored"), ("07", "pending"), ("0A", "permanent")):
                raw = provider.query(command)
                raw_responses[command] = raw
                payloads, frame_errors = parse_elm_response_scoped(raw)
                errors.extend({"command": command, **item} for item in frame_errors)
                if not payloads:
                    status[state] = "error" if frame_errors else "no_data"
                    continue
                decoded_responders = 0
                failed_responders = len(frame_errors)
                for ecu, payload in payloads.items():
                    try:
                        grouped[state].extend(
                            decode_dtc_payload(payload, state=state, ecu=ecu)  # type: ignore[arg-type]
                        )
                        decoded_responders += 1
                    except OBDProtocolError as exc:
                        failed_responders += 1
                        errors.append({"command": command, "ecu": ecu, "error": str(exc)})
                if decoded_responders and failed_responders:
                    status[state] = "partial"
                elif decoded_responders:
                    status[state] = "complete"
                else:
                    status[state] = "error"
            readiness_counts = {
                str(item["ecu"]): int(item["dtc_count"])
                for item in readiness
                if item.get("ecu") is not None and item.get("dtc_count") is not None
            }
            stored_counts: dict[str, int] = {}
            for item in grouped["stored"]:
                ecu = str(item["ecu"])
                stored_counts[ecu] = stored_counts.get(ecu, 0) + 1
            for ecu, reported_count in readiness_counts.items():
                decoded_count = stored_counts.get(ecu, 0)
                if decoded_count != reported_count:
                    if status.get("stored") in {"complete", "partial", "no_data"}:
                        status["stored"] = "partial"
                    errors.append({
                        "command": "03",
                        "ecu": ecu,
                        "error": (
                            f"PID 0101 reported {reported_count} confirmed DTCs but "
                            f"Mode 03 decoded {decoded_count}"
                        ),
                    })
            return {
                "observed_at": observed_at,
                "readiness": readiness,
                **grouped,
                "stored_status": status.get("stored", "no_data"),
                "pending_status": status.get("pending", "no_data"),
                "permanent_status": status.get("permanent", "no_data"),
                "errors": errors,
                "raw_responses": raw_responses,
            }

    def read_vehicle_info(self) -> dict[str, Any]:
        with self._lock:
            provider = self._require_provider()
            if self._identity is None:
                raise OBDNotConnected("OBD identity is unavailable")
            raw = provider.query("0902")
            payloads, frame_errors = parse_elm_response_scoped(raw)
            vins: list[dict[str, str]] = []
            errors: list[dict[str, str]] = [
                {"command": "0902", **item} for item in frame_errors
            ]
            for ecu, payload in sorted(payloads.items()):
                try:
                    vins.append({"ecu": ecu, "vin": decode_vin(payload)})
                except OBDProtocolError as exc:
                    errors.append({"command": "0902", "ecu": ecu, "error": str(exc)})
            distinct = {item["vin"] for item in vins}
            if vins and errors:
                vin_status = "partial"
            elif vins:
                vin_status = "complete"
            elif errors:
                vin_status = "error"
            else:
                vin_status = "no_data"
            return {
                "observed_at": _utc_now(),
                "vins": vins,
                "vin_status": vin_status,
                "vin_mismatch": len(distinct) > 1,
                "provider": self._identity["provider"],
                "adapter_identity": self._identity["adapter_identity"],
                "protocol": self._identity["protocol"],
                "responder_ids": list(self._identity.get("responder_ids", [])),
                "voltage": self._identity.get("voltage"),
                "errors": errors,
                "raw_responses": {"0902": raw},
            }

    def clear_faults(self) -> None:
        raise PermissionError(
            "hardware fault clearing requires an authenticated operator identity"
        )
