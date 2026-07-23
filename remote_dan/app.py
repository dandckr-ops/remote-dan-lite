from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from remote_dan import __version__
from remote_dan.can_analysis import NOMINAL_BITRATES
from remote_dan.bus_survey import (
    BusSurveyBackend,
    BusSurveyManager,
    BusSurveyRequest,
    BusSurveySimulatorBackend,
    PicoBusSurveyBackend,
)
from remote_dan.can_decode import (
    MAX_FRAME_LINE_BYTES,
    MAX_FRAMES_JSONL_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SCANNED_FRAME_LINES,
    MAX_SOURCE_BYTES,
    MAX_SUMMARY_BYTES,
    RUN_ID_PATTERN,
    CanDecodeManager,
    CanDecodeRequest,
    CanDecodeSourceNotFound,
    bus_survey_fast_sample_count,
    eligible_bus_survey_classification,
    read_authoritative_artifact,
    validated_artifact_path,
)
from remote_dan.capture import (
    ATTENUATIONS,
    COUPLINGS,
    INPUT_RANGES_V,
    PRESETS,
    SCOPE_PROFILES,
    CaptureBackend,
    CaptureManager,
    CaptureRequest,
    ScopeChannelConfig,
    SimulatorBackend,
    scope_config_payload,
)
from remote_dan.database import EvidenceDatabase
from remote_dan.hardware import probe_pico_hardware
from remote_dan.modbus_discovery import (
    MAX_SCAN_HOSTS,
    MAX_SCAN_WORKERS,
    bounded_scan_networks,
    connected_ipv4_networks,
)
from remote_dan.modbus_scan import (
    LiveModbusDiscoveryBackend,
    ModbusScanBackend,
    ModbusScanManager,
    ModbusScanRequest,
    ModbusSimulatorBackend,
)
from remote_dan.serial_analysis import SerialFraming
from remote_dan.usb_inventory import list_usb_devices
from remote_dan.routing_socket import RoutingSocketClient, RoutingSocketError
from remote_dan.serial_capture import (
    SerialCaptureBackend,
    SerialCaptureManager,
    SerialCaptureRequest,
    SerialSimulatorBackend,
    TermiosSerialBackend,
    probe_serial_hardware,
)


def _finite_nonnegative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _exact_identifier_hex(identifier: int, extended: bool) -> str:
    return f"0x{identifier:08X}" if extended else f"0x{identifier:03X}"


def _valid_payload_hex(value: object, *, max_bytes: int = 8) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= max_bytes * 2
        and len(value) % 2 == 0
        and value == value.upper()
        and all(character in "0123456789ABCDEF" for character in value)
    )


def _valid_identifier_summary(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    identifier = item.get("identifier")
    extended = item.get("extended")
    frame_count = item.get("frame_count")
    payload_changes = item.get("payload_change_count")
    first = item.get("first_timestamp_us")
    last = item.get("last_timestamp_us")
    byte_changes = item.get("byte_change_counts")
    if (
        not isinstance(identifier, int) or isinstance(identifier, bool)
        or not isinstance(extended, bool)
        or not 0 <= identifier <= (0x1FFFFFFF if extended else 0x7FF)
        or item.get("identifier_hex") != _exact_identifier_hex(identifier, extended)
        or not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count <= 0
        or not _finite_nonnegative(first) or not _finite_nonnegative(last)
        or float(last) < float(first)
        or not isinstance(payload_changes, int) or isinstance(payload_changes, bool)
        or not 0 <= payload_changes <= frame_count - 1
        or not _valid_payload_hex(item.get("last_payload_hex"))
        or not isinstance(byte_changes, list) or len(byte_changes) > 8
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= frame_count - 1
            for value in byte_changes
        )
    ):
        return False
    for name in ("observed_duration_us", "mean_period_us", "mean_frequency_hz",
                 "min_interval_us", "max_interval_us"):
        value = item.get(name)
        if value is not None and not _finite_nonnegative(value):
            return False
    return True


def _valid_can_frame(frame: object) -> bool:
    if not isinstance(frame, dict):
        return False
    identifier = frame.get("identifier")
    extended = frame.get("extended")
    dlc = frame.get("dlc")
    payload = frame.get("payload_bytes")
    start = frame.get("source_sample_start")
    end = frame.get("source_sample_end")
    if (
        not isinstance(identifier, int) or isinstance(identifier, bool)
        or not isinstance(extended, bool)
        or not 0 <= identifier <= (0x1FFFFFFF if extended else 0x7FF)
        or frame.get("identifier_hex") != _exact_identifier_hex(identifier, extended)
        or not isinstance(frame.get("remote"), bool)
        or not isinstance(dlc, int) or isinstance(dlc, bool) or not 0 <= dlc <= 15
        or not isinstance(payload, list)
        or any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255
               for value in payload)
        or frame.get("payload_hex") != bytes(payload).hex().upper()
        or not _valid_payload_hex(frame.get("payload_hex"))
        or (frame.get("remote") and payload != [])
        or (not frame.get("remote") and len(payload) != min(dlc, 8))
        or frame.get("crc_valid") is not True
        or frame.get("ack_slot") not in {"dominant", "recessive"}
        or frame.get("nominal_bitrate_bps") not in NOMINAL_BITRATES
        or not _finite_nonnegative(frame.get("timestamp_us"))
        or not isinstance(start, int) or isinstance(start, bool)
        or not isinstance(end, int) or isinstance(end, bool)
        or not 0 <= start < end
    ):
        return False
    return True


def _valid_can_decode_document(document: object, run_id: str) -> bool:
    if not isinstance(document, dict):
        return False
    source_run_id = document.get("source_run_id")
    source_capture_id = document.get("source_capture_id")
    source_capture_type = document.get("source_capture_type")
    source_profile = document.get("source_profile")
    source_artifact = document.get("source_artifact")
    source_sha256 = document.get("source_sha256")
    source_manifest_sha256 = document.get("source_manifest_sha256")
    source_parent_samples = document.get("source_parent_samples")
    source_samples = document.get("source_samples")
    frame_count = document.get("frame_count")
    identifier_count = document.get("identifier_count")
    writes_performed = document.get("writes_performed")
    return (
        document.get("run_id") == run_id
        and document.get("capture_type") == "can_decode"
        and isinstance(source_run_id, str)
        and RUN_ID_PATTERN.fullmatch(source_run_id) is not None
        and source_run_id not in {".", ".."}
        and isinstance(source_capture_id, int)
        and not isinstance(source_capture_id, bool)
        and source_capture_id > 0
        and source_capture_type in {"bus_survey", "can", "scope"}
        and (source_profile is None or isinstance(source_profile, str))
        and source_artifact in {"fast.csv", "capture.csv"}
        and isinstance(source_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_sha256) is not None
        and isinstance(source_manifest_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_manifest_sha256) is not None
        and isinstance(source_parent_samples, int)
        and not isinstance(source_parent_samples, bool)
        and source_parent_samples > 0
        and isinstance(source_samples, int)
        and not isinstance(source_samples, bool)
        and source_samples > 0
        and isinstance(frame_count, int)
        and not isinstance(frame_count, bool)
        and frame_count > 0
        and isinstance(identifier_count, int)
        and not isinstance(identifier_count, bool)
        and identifier_count > 0
        and document.get("can_polarity") in {"expected", "reversed"}
        and document.get("nominal_bitrate_bps") in NOMINAL_BITRATES
        and isinstance(writes_performed, int)
        and not isinstance(writes_performed, bool)
        and writes_performed == 0
    )


def _update_identifier_evidence(
    accumulators: dict[tuple[int, bool], dict[str, Any]],
    frame: dict[str, object],
) -> None:
    key = (int(frame["identifier"]), bool(frame["extended"]))
    timestamp = float(frame["timestamp_us"])
    payload = list(frame["payload_bytes"])
    payload_state = (bool(frame["remote"]), int(frame["dlc"]), tuple(payload))
    state = accumulators.get(key)
    if state is None:
        accumulators[key] = {
            "count": 1,
            "first": timestamp,
            "last": timestamp,
            "interval_sum": 0.0,
            "min_interval": None,
            "max_interval": None,
            "payload_state": payload_state,
            "payload": payload,
            "payload_changes": 0,
            "byte_changes": [0] * len(payload),
            "last_payload_hex": str(frame["payload_hex"]),
        }
        return
    interval = timestamp - float(state["last"])
    state["count"] = int(state["count"]) + 1
    state["interval_sum"] = float(state["interval_sum"]) + interval
    state["min_interval"] = (
        interval if state["min_interval"] is None
        else min(float(state["min_interval"]), interval)
    )
    state["max_interval"] = (
        interval if state["max_interval"] is None
        else max(float(state["max_interval"]), interval)
    )
    if payload_state != state["payload_state"]:
        state["payload_changes"] = int(state["payload_changes"]) + 1
    previous_payload = list(state["payload"])
    byte_changes = list(state["byte_changes"])
    maximum_length = max(len(previous_payload), len(payload))
    byte_changes.extend([0] * (maximum_length - len(byte_changes)))
    for index in range(maximum_length):
        previous_value = previous_payload[index] if index < len(previous_payload) else None
        current_value = payload[index] if index < len(payload) else None
        if previous_value != current_value:
            byte_changes[index] += 1
    state.update({
        "last": timestamp,
        "payload_state": payload_state,
        "payload": payload,
        "byte_changes": byte_changes,
        "last_payload_hex": str(frame["payload_hex"]),
    })


def _summarize_identifier_evidence(
    accumulators: dict[tuple[int, bool], dict[str, Any]],
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for (identifier, extended), state in sorted(accumulators.items()):
        count = int(state["count"])
        first = float(state["first"])
        last = float(state["last"])
        mean_period = float(state["interval_sum"]) / (count - 1) if count > 1 else None
        summaries.append({
            "identifier": identifier,
            "identifier_hex": _exact_identifier_hex(identifier, extended),
            "extended": extended,
            "frame_count": count,
            "first_timestamp_us": first,
            "last_timestamp_us": last,
            "observed_duration_us": last - first,
            "mean_period_us": mean_period,
            "mean_frequency_hz": 1_000_000.0 / mean_period if mean_period else None,
            "min_interval_us": state["min_interval"],
            "max_interval_us": state["max_interval"],
            "payload_change_count": int(state["payload_changes"]),
            "last_payload_hex": str(state["last_payload_hex"]),
            "byte_change_counts": list(state["byte_changes"]),
        })
    return summaries


STATIC_DIR = Path(__file__).with_name("static")
MAX_ARTIFACT_DOWNLOAD_BYTES = 64 * 1024 * 1024


class CapturePayload(BaseModel):
    label: str = Field(default="field capture", min_length=1, max_length=80)
    preset: Literal[
        "can-analysis", "short", "medium", "long", "1s", "2s", "5s", "10s"
    ] = "short"
    mode: Literal["auto", "hardware", "simulator"] = "auto"
    capture_type: Literal["scope", "serial", "can", "test"] = "scope"
    profile: Literal[
        "network",
        "general",
        "secondary-ignition",
        "crankshaft-vr",
        "crankshaft-hall",
        "injector-primary",
    ] = "network"
    session_id: int | None = Field(default=None, ge=1)
    test_type: str | None = Field(default=None, min_length=1, max_length=80)
    channels: list["ScopeChannelPayload"] | None = None


class ScopeChannelPayload(BaseModel):
    channel: Literal["A", "B", "C", "D"]
    enabled: bool = False
    label: str = Field(min_length=1, max_length=40)
    input_range_v: Literal[
        0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0
    ] = 20.0
    attenuation: Literal[1.0, 10.0, 20.0] = 1.0
    coupling: Literal["DC", "AC"] = "DC"


class SerialCapturePayload(BaseModel):
    label: str = Field(default="serial receive capture", min_length=1, max_length=80)
    duration_s: Literal[1, 2, 5, 10, 30] = 5
    mode: Literal["auto", "hardware", "simulator"] = "auto"
    baud: Literal[300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400] = 9600
    data_bits: Literal[5, 6, 7, 8] = 8
    parity: Literal["N", "E", "O"] = "N"
    stop_bits: Literal[1, 2] = 1
    session_id: int | None = Field(default=None, ge=1)


class BusSurveyPayload(BaseModel):
    label: str = Field(default="unknown bus survey", min_length=1, max_length=80)
    harness: Literal[
        "can-network", "protected-differential", "protected-single-ended"
    ]
    mode: Literal["hardware", "simulator"] = "simulator"
    session_id: int | None = Field(default=None, ge=1)
    low_voltage_confirmed: bool = False
    common_reference_confirmed: bool = False
    probe_rating_confirmed: bool = False
    passive_only_confirmed: bool = False


class CanDecodePayload(BaseModel):
    source_run_id: str = Field(min_length=1, max_length=128, pattern=r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    label: str = Field(default="CAN decode", min_length=1, max_length=80)


class ModbusScanPayload(BaseModel):
    label: str = Field(default="Modbus network discovery", min_length=1, max_length=80)
    interface: str = Field(min_length=1, max_length=32)
    subnet: str = Field(min_length=3, max_length=43)
    mode: Literal["network", "simulator"] = "network"
    connect_timeout_ms: int = Field(default=300, ge=100, le=750)
    response_timeout_ms: int = Field(default=1250, ge=500, le=1500)
    hicp_timeout_ms: int = Field(default=1500, ge=250, le=2000)
    workers: int = Field(default=4, ge=1, le=MAX_SCAN_WORKERS)
    session_id: int | None = Field(default=None, ge=1)


class UsbRoutingApplyPayload(BaseModel):
    inventory_revision: str = Field(min_length=64, max_length=64)
    routes: dict[str, Literal["local", "virtualhere"]]
    confirmed: bool = False


CapturePayload.model_rebuild()


def _scope_channel_overrides(payload: CapturePayload) -> tuple[ScopeChannelConfig, ...]:
    if payload.channels is None:
        return ()
    if payload.profile == "network":
        raise HTTPException(
            status_code=422,
            detail="the network profile uses the commissioned harness and cannot be overridden",
        )
    if len(payload.channels) != 4 or {item.channel for item in payload.channels} != {
        "A", "B", "C", "D"
    }:
        raise HTTPException(status_code=422, detail="scope channels must contain A, B, C, and D once")
    if not any(item.enabled for item in payload.channels):
        raise HTTPException(status_code=422, detail="at least one scope channel must be enabled")
    enabled_labels = [item.label.strip() for item in payload.channels if item.enabled]
    if len(enabled_labels) != len(set(enabled_labels)):
        raise HTTPException(status_code=422, detail="enabled scope channel labels must be unique")
    if any(
        item.enabled and item.attenuation == 20.0 and item.input_range_v > 20.0
        for item in payload.channels
    ):
        raise HTTPException(
            status_code=422,
            detail="20:1 attenuation is limited to a ±400 V external display range",
        )
    return tuple(
        ScopeChannelConfig(
            channel=item.channel,
            enabled=item.enabled,
            label=item.label.strip(),
            input_range_v=float(item.input_range_v),
            attenuation=float(item.attenuation),
            coupling=item.coupling,
        )
        for item in sorted(payload.channels, key=lambda item: item.channel)
    )


def _list_manifests(
    data_dir: Path,
    database: EvidenceDatabase,
) -> list[dict[str, object]]:
    manifests: list[dict[str, object]] = []
    for record in database.list_complete_captures(limit=200):
        try:
            data = read_authoritative_artifact(
                data_dir, record, "manifest.json", max_bytes=MAX_MANIFEST_BYTES
            )
            manifest = json.loads(data)
            if (
                not isinstance(manifest, dict)
                or manifest.get("run_id") != record.get("run_id")
                or manifest.get("capture_type") != record.get("capture_type")
            ):
                continue
            manifests.append(manifest)
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
            continue
    return manifests


def _list_can_decode_sources(
    data_dir: Path,
    database: EvidenceDatabase,
) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = []
    for record in database.list_complete_captures(
        capture_types=("can", "scope", "bus_survey"),
        limit=200,
    ):
        try:
            manifest = json.loads(read_authoritative_artifact(
                data_dir, record, "manifest.json", max_bytes=MAX_MANIFEST_BYTES
            ))
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        capture_type = manifest.get("capture_type")
        if capture_type != record.get("capture_type"):
            continue
        profile = manifest.get("profile")
        recorded_profile = record.get("metadata", {}).get("profile")
        if recorded_profile is not None and recorded_profile != profile:
            continue
        if (
            recorded_profile is None
            and not (capture_type == "bus_survey" and profile == "bus-sniffer")
        ):
            continue
        summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
        classification = summary.get("classification") if isinstance(summary, dict) else {}
        is_survey_can = (
            capture_type == "bus_survey"
            and eligible_bus_survey_classification(classification)
        )
        is_network = capture_type == "can" or (
            capture_type == "scope" and manifest.get("profile") == "network"
        )
        if not (is_survey_can or is_network):
            continue
        run_id = manifest.get("run_id")
        if (
            not isinstance(run_id, str)
            or run_id != record.get("run_id")
            or not RUN_ID_PATTERN.fullmatch(run_id)
        ):
            continue
        source_artifact = "fast.csv" if is_survey_can else "capture.csv"
        try:
            validated_artifact_path(data_dir, record, source_artifact)
        except ValueError:
            continue
        sources.append({
            "run_id": run_id,
            "capture_id": record["id"],
            "captured_at": record["captured_at"],
            "label": record["label"],
            "capture_type": capture_type,
            "source_artifact": source_artifact,
        })
        if len(sources) == 200:
            break
    return sources


def create_app(
    data_dir: Path | str = "/var/lib/remote-dan-lite/captures",
    db_path: Path | str | None = None,
    hardware_probe: Callable[[], dict[str, object]] = probe_pico_hardware,
    hardware_backend: CaptureBackend | None = None,
    serial_probe: Callable[[], dict[str, object]] = probe_serial_hardware,
    serial_backend: SerialCaptureBackend | None = None,
    network_probe: Callable[[], tuple[dict[str, str], ...]] = connected_ipv4_networks,
    modbus_backend: ModbusScanBackend | None = None,
    bus_survey_backend: BusSurveyBackend | None = None,
    usb_inventory_probe: Callable[[], list[dict[str, str | None]]] = list_usb_devices,
    routing_client: RoutingSocketClient | None = None,
) -> FastAPI:
    capture_dir = Path(data_dir)
    capture_dir.mkdir(parents=True, exist_ok=True)
    database = EvidenceDatabase(
        Path(db_path) if db_path is not None else capture_dir.with_suffix(".sqlite3")
    )
    database.initialize()
    pico_lock = threading.Lock()
    simulator = CaptureManager(
        capture_dir,
        backend=SimulatorBackend(),
        database=database,
    )
    serial_simulator = SerialCaptureManager(
        capture_dir,
        backend=SerialSimulatorBackend(),
        database=database,
    )
    modbus_simulator = ModbusScanManager(
        capture_dir,
        backend=ModbusSimulatorBackend(),
        database=database,
    )
    bus_survey_simulator = BusSurveyManager(
        capture_dir,
        backend=BusSurveySimulatorBackend(),
        database=database,
    )
    can_decode_manager = CanDecodeManager(capture_dir, database=database)

    app = FastAPI(
        title="Remote Dan Lite",
        version=__version__,
        description="Traceworks field capture appliance",
    )
    app.state.capture_dir = capture_dir
    app.state.database = database
    app.state.hardware_probe = hardware_probe
    app.state.serial_probe = serial_probe
    app.state.network_probe = network_probe
    app.state.usb_inventory_probe = usb_inventory_probe
    app.state.routing_client = routing_client or RoutingSocketClient(Path("/run/remote-dan-routing/control.sock"))
    app.state.simulator = simulator
    app.state.serial_simulator = serial_simulator
    app.state.modbus_simulator = modbus_simulator
    app.state.bus_survey_simulator = bus_survey_simulator
    app.state.can_decode_manager = can_decode_manager
    app.state.pico_lock = pico_lock
    app.state.serial_hardware_manager = (
        SerialCaptureManager(capture_dir, backend=serial_backend, database=database)
        if serial_backend is not None
        else None
    )
    app.state.modbus_network_manager = (
        ModbusScanManager(capture_dir, backend=modbus_backend, database=database)
        if modbus_backend is not None
        else None
    )
    app.state.hardware_manager = (
        CaptureManager(
            capture_dir,
            backend=hardware_backend,
            database=database,
            lock=pico_lock,
        )
        if hardware_backend is not None
        else None
    )
    initial_survey_backend = bus_survey_backend or (
        PicoBusSurveyBackend(hardware_backend) if hardware_backend is not None else None
    )
    app.state.bus_survey_hardware_manager = (
        BusSurveyManager(
            capture_dir,
            backend=initial_survey_backend,
            database=database,
            lock=pico_lock,
        )
        if initial_survey_backend is not None
        else None
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    @app.get("/artifacts/{run_id}/{filename}", include_in_schema=False)
    def artifact(run_id: str, filename: str) -> Response:
        if (
            not RUN_ID_PATTERN.fullmatch(run_id)
            or run_id in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename)
            or filename in {".", ".."}
        ):
            raise HTTPException(status_code=404, detail="artifact not found")
        record = database.get_capture_by_run_id(run_id)
        try:
            if record is None or record.get("status") != "complete":
                raise ValueError("artifact is not complete")
            matches = [
                item for item in record.get("artifacts", [])
                if item.get("filename") == filename
            ]
            if len(matches) != 1:
                raise ValueError("artifact registration is not unique")
            media_type = matches[0].get("media_type")
            if not isinstance(media_type, str) or not media_type:
                raise ValueError("artifact media type is invalid")
            data = read_authoritative_artifact(
                capture_dir, record, filename, max_bytes=MAX_ARTIFACT_DOWNLOAD_BYTES
            )
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="artifact not found") from None
        return Response(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> dict[str, object]:
        hardware = hardware_probe()
        serial_hardware = serial_probe()
        hardware_ready = bool(
            hardware.get("driver_available") and hardware.get("device_present")
        )
        return {
            "service": "remote-dan-lite",
            "version": __version__,
            "hostname": socket.gethostname(),
            "capture_ready": True,
            "default_backend": "hardware" if hardware_ready else "simulator",
            "hardware": hardware,
            "serial_hardware": serial_hardware,
        }

    @app.get("/api/captures")
    def captures() -> list[dict[str, object]]:
        return _list_manifests(capture_dir, database)

    @app.get("/api/can-decode-sources")
    def can_decode_sources() -> dict[str, object]:
        sources = _list_can_decode_sources(capture_dir, database)
        return {
            "sources": sources,
            "returned_count": len(sources),
            "source_limit": 200,
            "writes_enabled": False,
        }

    @app.post("/api/can-decodes", status_code=201)
    def create_can_decode(payload: CanDecodePayload) -> dict[str, object]:
        try:
            return app.state.can_decode_manager.run(CanDecodeRequest(
                source_run_id=payload.source_run_id,
                label=payload.label,
            ))
        except CanDecodeSourceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/can-decodes/{run_id}")
    def can_decode_result(
        run_id: str,
        identifier: str = "",
        changing_only: bool = False,
    ) -> dict[str, object]:
        if not RUN_ID_PATTERN.fullmatch(run_id) or run_id in {".", ".."}:
            raise HTTPException(status_code=404, detail="CAN decode not found")
        identifier_filter = identifier.strip().lower()
        if len(identifier_filter) > 16 or any(
            character not in "0123456789abcdefx" for character in identifier_filter
        ):
            raise HTTPException(status_code=422, detail="invalid CAN identifier filter")
        identifier_filter = identifier_filter.removeprefix("0x")
        record = database.get_capture_by_run_id(run_id)
        try:
            if (
                record is None
                or record.get("status") != "complete"
                or record.get("capture_type") != "can_decode"
            ):
                raise ValueError("not an authoritative CAN decode")
            frames_bytes = read_authoritative_artifact(
                capture_dir, record, "frames.jsonl", max_bytes=MAX_FRAMES_JSONL_BYTES
            )
            read_authoritative_artifact(
                capture_dir, record, "identifiers.csv", max_bytes=MAX_SUMMARY_BYTES
            )
            summary = json.loads(read_authoritative_artifact(
                capture_dir, record, "summary.json", max_bytes=MAX_SUMMARY_BYTES
            ))
            manifest = json.loads(read_authoritative_artifact(
                capture_dir, record, "manifest.json", max_bytes=MAX_MANIFEST_BYTES
            ))
            identity_fields = (
                "source_run_id",
                "source_capture_id",
                "source_capture_type",
                "source_profile",
                "source_artifact",
                "source_sha256",
                "source_manifest_sha256",
                "source_parent_samples",
                "source_samples",
                "can_polarity",
                "nominal_bitrate_bps",
                "writes_performed",
                "frame_count",
                "identifier_count",
            )
            if (
                not _valid_can_decode_document(manifest, run_id)
                or not _valid_can_decode_document(summary, run_id)
                or any(manifest.get(name) != summary.get(name) for name in identity_fields)
            ):
                raise OSError("not a CAN decode")
            source_record = database.get_capture_by_run_id(str(manifest["source_run_id"]))
            recorded_source_profile = (
                source_record.get("metadata", {}).get("profile")
                if source_record is not None
                else None
            )
            if (
                source_record is None
                or source_record.get("status") != "complete"
                or source_record.get("id") != manifest["source_capture_id"]
                or source_record.get("capture_type") != manifest["source_capture_type"]
                or source_record.get("samples") != manifest["source_parent_samples"]
                or (
                    recorded_source_profile is not None
                    and recorded_source_profile != manifest["source_profile"]
                )
                or (
                    recorded_source_profile is None
                    and not (
                        manifest["source_capture_type"] == "bus_survey"
                        and manifest["source_profile"] == "bus-sniffer"
                    )
                )
            ):
                raise OSError("CAN decode parent lineage is not authoritative")
            source_manifest_bytes = read_authoritative_artifact(
                capture_dir, source_record, "manifest.json", max_bytes=MAX_MANIFEST_BYTES
            )
            source_bytes = read_authoritative_artifact(
                capture_dir, source_record, str(manifest["source_artifact"]),
                max_bytes=MAX_SOURCE_BYTES,
            )
            source_manifest = json.loads(source_manifest_bytes)
            if (
                hashlib.sha256(source_manifest_bytes).hexdigest()
                != manifest["source_manifest_sha256"]
                or hashlib.sha256(source_bytes).hexdigest() != manifest["source_sha256"]
                or not isinstance(source_manifest, dict)
                or source_manifest.get("run_id") != manifest["source_run_id"]
                or source_manifest.get("capture_type") != manifest["source_capture_type"]
                or source_manifest.get("profile") != manifest["source_profile"]
                or not isinstance(source_manifest.get("sha256"), dict)
                or source_manifest["sha256"].get(manifest["source_artifact"])
                != manifest["source_sha256"]
            ):
                raise OSError("CAN decode parent artifacts are not authoritative")
            source_type = str(manifest["source_capture_type"])
            source_profile = manifest["source_profile"]
            expected_source_artifact = "capture.csv"
            if source_type == "bus_survey":
                expected_source_artifact = "fast.csv"
                source_summary = source_manifest.get("summary")
                classification = (
                    source_summary.get("classification", {})
                    if isinstance(source_summary, dict)
                    else {}
                )
                if not eligible_bus_survey_classification(classification):
                    raise OSError("CAN decode parent is no longer eligible")
                if (
                    source_manifest.get("samples") != manifest["source_parent_samples"]
                    or bus_survey_fast_sample_count(source_manifest)
                    != manifest["source_samples"]
                ):
                    raise OSError("CAN decode bus survey sample authority is inconsistent")
            elif source_type == "can":
                if manifest["source_parent_samples"] != manifest["source_samples"]:
                    raise OSError("CAN decode parent sample authority is inconsistent")
            elif source_type == "scope" and source_profile == "network":
                if manifest["source_parent_samples"] != manifest["source_samples"]:
                    raise OSError("CAN decode parent sample authority is inconsistent")
            else:
                raise OSError("CAN decode parent is no longer eligible")
            if manifest["source_artifact"] != expected_source_artifact:
                raise OSError("CAN decode parent artifact selection is inconsistent")
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(status_code=404, detail="CAN decode not found") from None
        all_identifiers = summary.get("identifiers", [])
        if (
            not isinstance(all_identifiers, list)
            or any(not _valid_identifier_summary(item) for item in all_identifiers)
        ):
            raise HTTPException(status_code=404, detail="CAN decode not found")
        summary_by_key = {
            (int(item["identifier"]), bool(item["extended"])): item
            for item in all_identifiers
        }
        if len(summary_by_key) != len(all_identifiers):
            raise HTTPException(status_code=404, detail="CAN decode not found")

        def identifier_matches(item: dict[str, object]) -> bool:
            normalized = str(item.get("identifier_hex", "")).lower().removeprefix("0x")
            return not identifier_filter or identifier_filter in normalized

        filtered_identifiers = [
            item for item in all_identifiers
            if isinstance(item, dict)
            and identifier_matches(item)
            and (not changing_only or int(item.get("payload_change_count", 0)) > 0)
        ]
        changing_keys = {
            (int(item["identifier"]), bool(item.get("extended")))
            for item in filtered_identifiers
        }
        frames: list[dict[str, object]] = []
        total_frames = 0
        scanned_frames = 0
        frame_counts: dict[tuple[int, bool], int] = {}
        previous_order: tuple[float, int, bool] | None = None
        previous_source_bounds: tuple[int, int] | None = None
        identifier_evidence: dict[tuple[int, bool], dict[str, Any]] = {}
        expected_bitrate = int(manifest["nominal_bitrate_bps"])
        source_samples = int(manifest["source_samples"])
        try:
            lines = frames_bytes.splitlines()
            if len(lines) > MAX_SCANNED_FRAME_LINES:
                raise ValueError("too many CAN frame rows")
            for line in lines:
                if not line or len(line) > MAX_FRAME_LINE_BYTES:
                    raise ValueError("invalid CAN frame line size")
                frame = json.loads(line)
                if not _valid_can_frame(frame):
                    raise ValueError("malformed CAN frame row")
                key = (int(frame["identifier"]), bool(frame["extended"]))
                order = (float(frame["timestamp_us"]), key[0], key[1])
                if previous_order is not None and order < previous_order:
                    raise ValueError("CAN frame rows are not chronological")
                previous_order = order
                source_bounds = (
                    int(frame["source_sample_start"]),
                    int(frame["source_sample_end"]),
                )
                if (
                    frame["nominal_bitrate_bps"] != expected_bitrate
                    or source_bounds[1] > source_samples
                    or previous_source_bounds is not None
                    and (
                        source_bounds[0] < previous_source_bounds[0]
                        or source_bounds[1] < previous_source_bounds[1]
                    )
                ):
                    raise ValueError("CAN frame source evidence is inconsistent")
                previous_source_bounds = source_bounds
                if key not in summary_by_key:
                    raise ValueError("CAN frame has no identifier summary")
                scanned_frames += 1
                frame_counts[key] = frame_counts.get(key, 0) + 1
                _update_identifier_evidence(identifier_evidence, frame)
                if not identifier_matches(frame):
                    continue
                if changing_only and (
                    int(frame.get("identifier", -1)),
                    bool(frame.get("extended")),
                ) not in changing_keys:
                    continue
                total_frames += 1
                if len(frames) < 200:
                    frames.append(frame)
            if any(
                frame_counts.get(key, 0) != int(item["frame_count"])
                for key, item in summary_by_key.items()
            ):
                raise ValueError("CAN identifier frame count mismatch")
            if all_identifiers != _summarize_identifier_evidence(identifier_evidence):
                raise ValueError("CAN identifier summary does not match frame evidence")
            for source in (manifest, summary):
                if "frame_count" in source and (
                    not isinstance(source["frame_count"], int)
                    or isinstance(source["frame_count"], bool)
                    or source["frame_count"] != scanned_frames
                ):
                    raise ValueError("CAN total frame count mismatch")
                if "identifier_count" in source and (
                    not isinstance(source["identifier_count"], int)
                    or isinstance(source["identifier_count"], bool)
                    or source["identifier_count"] != len(all_identifiers)
                ):
                    raise ValueError("CAN identifier count mismatch")
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(status_code=404, detail="CAN decode not found") from None
        identifiers = filtered_identifiers[:200]
        total_identifiers = len(filtered_identifiers)
        return {
            "run_id": run_id,
            "capture_id": record["id"],
            "source_run_id": manifest.get("source_run_id"),
            "can_polarity": manifest.get("can_polarity"),
            "nominal_bitrate_bps": manifest.get("nominal_bitrate_bps"),
            "writes_performed": 0,
            "identifier_filter": identifier,
            "changing_only": changing_only,
            "frame_limit": 200,
            "total_frame_count": total_frames,
            "returned_frame_count": len(frames),
            "frames_truncated": total_frames > len(frames),
            "frames": frames,
            "identifier_limit": 200,
            "total_identifier_count": total_identifiers,
            "returned_identifier_count": len(identifiers),
            "identifiers_truncated": total_identifiers > len(identifiers),
            "identifiers": identifiers,
            "warnings": manifest.get("warnings", []),
            "limitations": manifest.get("limitations", []),
            "artifact_urls": {
                "frames_jsonl": f"/artifacts/{run_id}/frames.jsonl",
                "identifiers_csv": f"/artifacts/{run_id}/identifiers.csv",
            },
        }

    @app.post("/api/bus-surveys", status_code=201)
    def create_bus_survey(payload: BusSurveyPayload) -> dict[str, object]:
        request = BusSurveyRequest(
            label=payload.label,
            harness=payload.harness,
            mode=payload.mode,
            session_id=payload.session_id,
            low_voltage_confirmed=payload.low_voltage_confirmed,
            common_reference_confirmed=payload.common_reference_confirmed,
            probe_rating_confirmed=payload.probe_rating_confirmed,
            passive_only_confirmed=payload.passive_only_confirmed,
        )
        if payload.mode == "simulator":
            try:
                return app.state.bus_survey_simulator.run(request)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        hardware = hardware_probe()
        hardware_ready = bool(
            hardware.get("driver_available") and hardware.get("device_present")
        )
        if not hardware_ready:
            raise HTTPException(status_code=503, detail=str(hardware.get("reason")))
        if app.state.bus_survey_hardware_manager is None:
            if app.state.hardware_manager is None:
                from remote_dan.pico import PicoPS2000ABackend

                pico_backend = PicoPS2000ABackend()
                app.state.hardware_manager = CaptureManager(
                    capture_dir,
                    backend=pico_backend,
                    database=database,
                    lock=pico_lock,
                )
            else:
                pico_backend = app.state.hardware_manager.backend
            app.state.bus_survey_hardware_manager = BusSurveyManager(
                capture_dir,
                backend=PicoBusSurveyBackend(pico_backend),
                database=database,
                lock=pico_lock,
            )
        try:
            return app.state.bus_survey_hardware_manager.run(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Bus survey acquisition failed: {exc}",
            ) from exc

    @app.get("/api/modbus/networks")
    def modbus_networks() -> dict[str, object]:
        try:
            networks = network_probe()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Network inventory failed: {exc}") from exc
        return {
            "networks": list(bounded_scan_networks(networks)),
            "policy": {
                "ipv4_only": True,
                "connected_subnets_only": True,
                "max_hosts": MAX_SCAN_HOSTS,
                "max_workers": MAX_SCAN_WORKERS,
                "cooldown_seconds": 60,
                "deadline_seconds": 30,
                "writes_enabled": False,
            },
        }

    @app.post("/api/modbus/scans", status_code=201)
    def create_modbus_scan(payload: ModbusScanPayload) -> dict[str, object]:
        try:
            networks = network_probe()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Network inventory failed: {exc}") from exc
        request = ModbusScanRequest(
            label=payload.label,
            subnet=payload.subnet,
            interface=payload.interface,
            connected_networks=networks,
            connect_timeout_s=payload.connect_timeout_ms / 1000.0,
            response_timeout_s=payload.response_timeout_ms / 1000.0,
            hicp_timeout_s=payload.hicp_timeout_ms / 1000.0,
            workers=payload.workers,
            mode=payload.mode,
            session_id=payload.session_id,
        )
        manager = app.state.modbus_simulator
        if payload.mode == "network":
            if app.state.modbus_network_manager is None:
                app.state.modbus_network_manager = ModbusScanManager(
                    capture_dir,
                    backend=LiveModbusDiscoveryBackend(),
                    database=database,
                )
            manager = app.state.modbus_network_manager
        try:
            return manager.run(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"Modbus discovery failed: {exc}") from exc

    @app.get("/api/usb/devices")
    def usb_devices() -> dict[str, object]:
        try:
            devices = app.state.usb_inventory_probe()
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"USB inventory failed: {exc}") from exc
        try:
            control = app.state.routing_client.request({"action": "status"})
        except (OSError, RoutingSocketError):
            control = {"available": False, "reason": "USB routing helper is not commissioned on this console yet."}
        allowed_devices = set(control.get("allowed_devices", [])) if control.get("available") else set()
        rendered_devices = [
            {
                **device,
                "route": (
                    "virtualhere"
                    if f"{device.get('vendor_id')}/{device.get('product_id')}" in allowed_devices
                    else "local"
                ),
            }
            for device in devices
        ]
        return {"devices": rendered_devices, "routing_control": control}

    @app.post("/api/usb/routing/apply")
    def apply_usb_routing(payload: UsbRoutingApplyPayload) -> dict[str, object]:
        if not payload.confirmed:
            raise HTTPException(status_code=422, detail="explicit routing confirmation is required")
        if pico_lock.locked():
            raise HTTPException(status_code=409, detail="a local scope or bus capture is active")
        manager = app.state.serial_hardware_manager
        if manager is not None and manager._lock.locked():
            raise HTTPException(status_code=409, detail="a local SEL serial capture is active")
        try:
            return app.state.routing_client.request({"action": "apply", "inventory_revision": payload.inventory_revision, "routes": payload.routes})
        except (OSError, RoutingSocketError) as exc:
            raise HTTPException(status_code=503, detail=f"USB routing apply failed: {exc}") from exc

    @app.post("/api/serial/captures", status_code=201)
    def create_serial_capture(payload: SerialCapturePayload) -> dict[str, object]:
        framing = SerialFraming(
            baud=payload.baud,
            data_bits=payload.data_bits,
            parity=payload.parity,
            stop_bits=payload.stop_bits,
        )
        serial_hardware = serial_probe()
        hardware_ready = bool(serial_hardware.get("device_present"))
        requested_hardware = payload.mode == "hardware" or (
            payload.mode == "auto" and hardware_ready
        )
        request = SerialCaptureRequest(
            label=payload.label,
            duration_s=float(payload.duration_s),
            framing=framing,
            mode="hardware" if requested_hardware else "simulator",
            session_id=payload.session_id,
        )
        if not requested_hardware:
            try:
                return app.state.serial_simulator.run(request)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not hardware_ready:
            raise HTTPException(status_code=503, detail=str(serial_hardware.get("reason")))
        if app.state.serial_hardware_manager is None:
            stable_path = serial_hardware.get("stable_path")
            if not stable_path:
                raise HTTPException(status_code=503, detail="SEL C662 stable device path is unavailable")
            app.state.serial_hardware_manager = SerialCaptureManager(
                capture_dir,
                backend=TermiosSerialBackend(Path(str(stable_path))),
                database=database,
            )
        try:
            return app.state.serial_hardware_manager.run(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"Serial receive failed: {exc}") from exc

    @app.get("/api/scope/profiles")
    def scope_profiles() -> dict[str, object]:
        return {
            "profiles": [
                {
                    "name": profile.name,
                    "label": profile.label,
                    "preset": profile.preset,
                    "description": profile.description,
                    "warning": profile.warning,
                    "channels": scope_config_payload(profile.channels),
                }
                for profile in SCOPE_PROFILES.values()
            ],
            "presets": {
                name: {
                    "samples": preset.samples,
                    "sample_interval_us": preset.sample_interval_us,
                    "duration_ms": preset.duration_ms,
                }
                for name, preset in PRESETS.items()
            },
            "input_ranges_v": list(INPUT_RANGES_V),
            "attenuations": list(ATTENUATIONS),
            "couplings": list(COUPLINGS),
        }

    @app.post("/api/captures", status_code=201)
    def create_capture(payload: CapturePayload) -> dict[str, object]:
        if payload.preset == "can-analysis" and payload.profile != "network":
            raise HTTPException(
                status_code=422,
                detail="the CAN analysis window is limited to the commissioned network harness",
            )
        channel_overrides = _scope_channel_overrides(payload)
        hardware = hardware_probe()
        hardware_ready = bool(
            hardware.get("driver_available") and hardware.get("device_present")
        )
        requested_hardware = payload.mode == "hardware" or (
            payload.mode == "auto" and hardware_ready
        )
        if requested_hardware:
            if not hardware_ready:
                raise HTTPException(status_code=503, detail=str(hardware.get("reason")))
            if app.state.hardware_manager is None:
                from remote_dan.pico import PicoPS2000ABackend

                app.state.hardware_manager = CaptureManager(
                    capture_dir,
                    backend=PicoPS2000ABackend(),
                    database=database,
                    lock=pico_lock,
                )
            try:
                return app.state.hardware_manager.run(
                    CaptureRequest(
                        label=payload.label,
                        preset=payload.preset,
                        mode="hardware",
                        capture_type=payload.capture_type,
                        session_id=payload.session_id,
                        test_type=payload.test_type,
                        profile=payload.profile,
                        channels=channel_overrides,
                    )
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"PS2000A acquisition failed: {exc}",
                ) from exc
        try:
            return simulator.run(
                CaptureRequest(
                    label=payload.label,
                    preset=payload.preset,
                    mode="simulator",
                    capture_type=payload.capture_type,
                    session_id=payload.session_id,
                    test_type=payload.test_type,
                    profile=payload.profile,
                    channels=channel_overrides,
                )
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/captures/{run_id}")
    def capture_manifest(run_id: str) -> dict[str, object]:
        record = database.get_capture_by_run_id(run_id)
        try:
            if record is None or record.get("status") != "complete":
                raise ValueError("capture is not complete")
            manifest = json.loads(read_authoritative_artifact(
                capture_dir, record, "manifest.json", max_bytes=MAX_MANIFEST_BYTES
            ))
            if (
                not isinstance(manifest, dict)
                or manifest.get("run_id") != run_id
                or manifest.get("capture_id") != record.get("id")
                or manifest.get("capture_type") != record.get("capture_type")
            ):
                raise ValueError("capture manifest does not match authoritative SQLite")
            return manifest
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(status_code=404, detail="capture not found") from None

    @app.get("/api/evidence/captures/{capture_id}")
    def evidence_capture(capture_id: int) -> dict[str, object]:
        record = database.get_capture(capture_id)
        if record is None:
            raise HTTPException(status_code=404, detail="capture not found")
        return record

    return app


def main() -> None:
    import uvicorn

    data_dir = Path(os.environ.get("REMOTE_DAN_DATA_DIR", "/var/lib/remote-dan-lite/captures"))
    db_path = Path(
        os.environ.get("REMOTE_DAN_DB_PATH", "/var/lib/remote-dan-lite/remote-dan.sqlite3")
    )
    host = os.environ.get("REMOTE_DAN_HOST", "0.0.0.0")
    port = int(os.environ.get("REMOTE_DAN_PORT", "8776"))
    uvicorn.run(
        create_app(data_dir=data_dir, db_path=db_path),
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
