from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from remote_dan import __version__
from remote_dan.can_analysis import (
    NOMINAL_BITRATES,
    aggregate_can_identifiers,
    build_can_diagnostics,
    build_can_payload_heatmap,
    build_can_timeline,
    compare_can_decode_results,
    decode_can_waveform,
)
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
from remote_dan.loadbank_client import (
    CollectorHttpError,
    CollectorUnavailableError,
    LoadBankClient,
    load_client_from_environment,
)
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
from remote_dan.obd_evidence import OBDEvidenceManager
from remote_dan.obd_protocol import OBDAdapterError
from remote_dan.obd_provider import (
    OBDInUse,
    OBDNotConnected,
    OBDProvider,
    OBDProviderError,
    OBDTimeout,
)
from remote_dan.obd_service import OBDService
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


def _valid_identifier_summary(item: object, *, artifact_schema_version: int = 2) -> bool:
    if not isinstance(item, dict):
        return False
    identifier = item.get("identifier")
    extended = item.get("extended")
    frame_count = item.get("frame_count")
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
        or not _valid_payload_hex(item.get("last_payload_hex"))
        or not isinstance(byte_changes, list) or len(byte_changes) > 8
    ):
        return False
    if artifact_schema_version == 1:
        payload_changes = item.get("payload_change_count")
        return (
            isinstance(payload_changes, int)
            and not isinstance(payload_changes, bool)
            and 0 <= payload_changes <= frame_count - 1
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= frame_count - 1
                for value in byte_changes
            )
        )
    interval_count = item.get("interval_count")
    state_changes = item.get("payload_state_change_count")
    bit_changes = item.get("bit_change_counts")
    if (
        not isinstance(interval_count, int) or isinstance(interval_count, bool)
        or interval_count != frame_count - 1
        or not isinstance(state_changes, int) or isinstance(state_changes, bool)
        or not 0 <= state_changes <= interval_count
        or not isinstance(bit_changes, list) or len(bit_changes) != len(byte_changes)
        or item.get("inter_arrival_stddev_measure")
        != "population standard deviation; reported only with at least 3 intervals"
    ):
        return False
    for name in (
        "payload_state_transition_count", "dlc_transition_count",
        "rtr_data_transition_count", "comparable_payload_transition_count",
        "comparable_payload_change_count", "introduced_byte_count", "removed_byte_count",
    ):
        value = item.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    for name in (
        "observed_duration_us", "mean_period_us", "mean_frequency_hz",
        "min_interval_us", "median_interval_us", "max_interval_us",
        "inter_arrival_stddev_us", "payload_state_change_percent",
    ):
        value = item.get(name)
        if value is not None and not _finite_nonnegative(value):
            return False
    if interval_count < 3 and item.get("inter_arrival_stddev_us") is not None:
        return False
    return all(
        isinstance(byte_counts, list)
        and len(byte_counts) == 8
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= interval_count
            for value in byte_counts
        )
        for byte_counts in bit_changes
    )


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
    artifact_schema_version = document.get("artifact_schema_version", 1)
    version_fields_valid = (
        artifact_schema_version == 1
        and "artifact_schema_version" not in document
        and "physical_layer_diagnostics" not in document
        and "integrity_diagnostics" not in document
    ) or (
        artifact_schema_version == 2
        and document.get("decoder_algorithm_version") == 2
        and document.get("analyzer_version") == 2
        and isinstance(document.get("decoder_settings"), dict)
        and document["decoder_settings"].get("classical_can_only") is True
    )
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
        version_fields_valid
        and document.get("run_id") == run_id
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
            "interval_square_sum": 0.0,
            "min_interval": None,
            "max_interval": None,
            "payload_state": payload_state,
            "payload": payload,
            "payload_changes": 0,
            "byte_changes": [0] * len(payload),
            "bit_changes": [[0] * 8 for _ in payload],
            "last_payload_hex": str(frame["payload_hex"]),
        }
        return
    interval = timestamp - float(state["last"])
    state["count"] = int(state["count"]) + 1
    state["interval_sum"] = float(state["interval_sum"]) + interval
    state["interval_square_sum"] = float(state["interval_square_sum"]) + interval * interval
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
    bit_changes = [list(counts) for counts in state["bit_changes"]]
    maximum_length = max(len(previous_payload), len(payload))
    byte_changes.extend([0] * (maximum_length - len(byte_changes)))
    bit_changes.extend([[0] * 8 for _ in range(maximum_length - len(bit_changes))])
    for index in range(maximum_length):
        previous_value = previous_payload[index] if index < len(previous_payload) else None
        current_value = payload[index] if index < len(payload) else None
        if previous_value != current_value:
            byte_changes[index] += 1
            if previous_value is None or current_value is None:
                bit_changes[index] = [count + 1 for count in bit_changes[index]]
            else:
                changed = previous_value ^ current_value
                for bit_index in range(8):
                    if changed & (1 << (7 - bit_index)):
                        bit_changes[index][bit_index] += 1
    state.update({
        "last": timestamp,
        "payload_state": payload_state,
        "payload": payload,
        "byte_changes": byte_changes,
        "bit_changes": bit_changes,
        "last_payload_hex": str(frame["payload_hex"]),
    })


def _summarize_identifier_evidence(
    accumulators: dict[tuple[int, bool], dict[str, Any]],
) -> list[dict[str, object]]:
    """Recompute the immutable v1 identifier schema for legacy evidence."""
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
    mode: Literal["auto", "hardware", "simulator"] = "hardware"
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
    mode: Literal["auto", "hardware", "simulator"] = "hardware"
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
    mode: Literal["hardware", "simulator"] = "hardware"
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


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyLoadBankPayload(StrictPayload):
    pass


class LoadBankOwnershipPayload(StrictPayload):
    owner: Literal["rdl", "windows", "off"]
    confirmed_external_stopped: bool


class LoadBankMetadataPayload(StrictPayload):
    customer: str = Field(min_length=1, max_length=120)
    work_order: str = Field(min_length=1, max_length=80)
    generator: str = Field(min_length=1, max_length=120)
    technician: str = Field(min_length=1, max_length=120)


class LoadBankSessionPayload(StrictPayload):
    candidate_id: str = Field(min_length=1, max_length=200)
    duration_minutes: int = Field(ge=15, le=1440, multiple_of=15)
    metadata: LoadBankMetadataPayload


class CustomerPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    company: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("customer name is required")
        return normalized


class VehiclePayload(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    vin: str | None = Field(default=None, min_length=1, max_length=32)
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    year: int | None = Field(default=None, ge=1886, le=2200)
    engine: str | None = Field(default=None, max_length=120)
    asset_tag: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("vehicle display name is required")
        return normalized


class DiagnosticSessionPayload(BaseModel):
    customer_id: int = Field(ge=1)
    vehicle_id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=240)
    complaint: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=240)
    operator_name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)


class OBDConnectPayload(BaseModel):
    mode: Literal["hardware"] = "hardware"
    session_id: int | None = Field(default=None, ge=1)


class OBDSnapshotPayload(BaseModel):
    kind: Literal["live", "faults", "vehicle_info"]
    label: str = Field(default="OBD snapshot", min_length=1, max_length=80)
    operation_id: UUID


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
    obd_simulator_provider: OBDProvider | None = None,
    obd_hardware_provider_factory: Callable[[], OBDProvider] | None = None,
    loadbank_client: LoadBankClient | None = None,
    loadbank_allowed_origins: set[str] | frozenset[str] | None = None,
) -> FastAPI:
    capture_dir = Path(data_dir)
    capture_dir.mkdir(parents=True, exist_ok=True)
    database = EvidenceDatabase(
        Path(db_path) if db_path is not None else capture_dir.with_suffix(".sqlite3")
    )
    database.initialize()
    pico_lock = threading.Lock()
    usb_routing_obd_lock = threading.Lock()
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
    obd_service = OBDService(
        database=database,
        simulator_provider=obd_simulator_provider,
        hardware_provider_factory=obd_hardware_provider_factory,
    )
    obd_evidence = OBDEvidenceManager(
        capture_dir,
        database=database,
        service=obd_service,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if obd_service.status()["connected"]:
            try:
                obd_service.disconnect()
            except Exception:
                pass

    app = FastAPI(
        title="Remote Dan Lite",
        version=__version__,
        description="Traceworks field capture appliance",
        lifespan=lifespan,
    )
    app.state.capture_dir = capture_dir
    app.state.database = database
    app.state.hardware_probe = hardware_probe
    app.state.serial_probe = serial_probe
    app.state.network_probe = network_probe
    app.state.usb_inventory_probe = usb_inventory_probe
    app.state.routing_client = routing_client or RoutingSocketClient(Path("/run/remote-dan-routing/control.sock"))
    app.state.loadbank_client = loadbank_client
    app.state.loadbank_allowed_origins = frozenset(
        loadbank_allowed_origins
        if loadbank_allowed_origins is not None
        else (
            origin.strip()
            for origin in os.environ.get(
                "REMOTE_DAN_LOADBANK_ALLOWED_ORIGINS", ""
            ).split(",")
            if origin.strip()
        )
    )
    app.state.simulator = simulator
    app.state.serial_simulator = serial_simulator
    app.state.modbus_simulator = modbus_simulator
    app.state.bus_survey_simulator = bus_survey_simulator
    app.state.can_decode_manager = can_decode_manager
    app.state.obd_service = obd_service
    app.state.obd_evidence = obd_evidence
    app.state.pico_lock = pico_lock
    app.state.usb_routing_obd_lock = usb_routing_obd_lock

    @app.middleware("http")
    async def protect_loadbank_mutations(request: Request, call_next: Callable) -> Response:
        is_mutation = (
            request.url.path.startswith("/api/loadbank/")
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        )
        if is_mutation:
            origin = request.headers.get("origin")
            if origin not in app.state.loadbank_allowed_origins:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Load Bank mutation origin is not allowed"},
                )
            media_type = request.headers.get("content-type", "").partition(";")[0]
            if media_type.strip().lower() != "application/json":
                return JSONResponse(
                    status_code=415,
                    content={"detail": "Load Bank mutations require application/json"},
                )
        return await call_next(request)

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
            "default_backend": "hardware" if hardware_ready else "unavailable",
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

    def _load_authoritative_can_decode_result(
        run_id: str,
        identifier: str = "",
        changing_only: bool = False,
        identifier_limit: int | None = 200,
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
            identifiers_bytes = read_authoritative_artifact(
                capture_dir, record, "identifiers.csv", max_bytes=MAX_SUMMARY_BYTES
            )
            summary_bytes = read_authoritative_artifact(
                capture_dir, record, "summary.json", max_bytes=MAX_SUMMARY_BYTES
            )
            summary = json.loads(summary_bytes)
            manifest = json.loads(read_authoritative_artifact(
                capture_dir, record, "manifest.json", max_bytes=MAX_MANIFEST_BYTES
            ))
            artifact_schema_version = int(manifest.get("artifact_schema_version", 1))
            if int(summary.get("artifact_schema_version", 1)) != artifact_schema_version:
                raise OSError("CAN decode artifact schema versions disagree")
            identity_fields = (
                "artifact_schema_version",
                "decoder_algorithm_version",
                "analyzer_version",
                "decoder_settings",
                "capture_id",
                "captured_at",
                "source_run_id",
                "source_captured_at",
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
                "physical_layer_diagnostics",
                "integrity_diagnostics",
            )
            if (
                not _valid_can_decode_document(manifest, run_id)
                or not _valid_can_decode_document(summary, run_id)
                or any(manifest.get(name) != summary.get(name) for name in identity_fields)
            ):
                raise OSError("not a CAN decode")
            if artifact_schema_version == 2 and (
                manifest.get("capture_id") != record.get("id")
                or manifest.get("captured_at") != record.get("captured_at")
                or manifest.get("summary") != summary
                or manifest.get("artifacts")
                != ["frames.jsonl", "identifiers.csv", "summary.json", "manifest.json"]
                or manifest.get("sha256") != {
                    "frames.jsonl": hashlib.sha256(frames_bytes).hexdigest(),
                    "identifiers.csv": hashlib.sha256(identifiers_bytes).hexdigest(),
                    "summary.json": hashlib.sha256(summary_bytes).hexdigest(),
                }
            ):
                raise OSError("CAN decode manifest declarations are not authoritative")
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
                    artifact_schema_version == 2
                    and manifest.get("source_captured_at") != source_record.get("captured_at")
                )
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
            source_time = None
            if artifact_schema_version == 2:
                source_time, source_can_h, source_can_l = (
                    CanDecodeManager._load_waveform_bytes(source_bytes)
                )
                source_decoded = decode_can_waveform(
                    source_time, source_can_h, source_can_l
                )
                expected_diagnostics = build_can_diagnostics(
                    source_time, source_can_h, source_can_l, source_decoded
                )
                expected_decode_metadata = {
                    "can_polarity": source_decoded["polarity"],
                    "nominal_bitrate_bps": source_decoded["nominal_bitrate_bps"],
                    "rejected_candidate_count": int(
                        source_decoded["rejected_candidate_count"]
                    ),
                    "unsupported_fd_candidate_count": int(
                        source_decoded.get("unsupported_fd_candidate_count", 0)
                    ),
                    "warnings": list(source_decoded["warnings"]),
                    "writes_performed": 0,
                }
                if any(
                    manifest.get(name) != expected_diagnostics[name]
                    or summary.get(name) != expected_diagnostics[name]
                    for name in (
                        "physical_layer_diagnostics", "integrity_diagnostics",
                    )
                ):
                    raise OSError("CAN decode diagnostics contradict source evidence")
                if any(
                    manifest.get(name) != value or summary.get(name) != value
                    for name, value in expected_decode_metadata.items()
                ):
                    raise OSError("CAN decode metadata contradicts source evidence")
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(status_code=404, detail="CAN decode not found") from None
        all_identifiers = summary.get("identifiers", [])
        if (
            not isinstance(all_identifiers, list)
            or any(
                not _valid_identifier_summary(
                    item, artifact_schema_version=artifact_schema_version
                )
                for item in all_identifiers
            )
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

        changing_field = (
            "payload_state_change_count"
            if artifact_schema_version == 2
            else "payload_change_count"
        )
        filtered_identifiers = [
            item for item in all_identifiers
            if isinstance(item, dict)
            and identifier_matches(item)
            and (not changing_only or int(item.get(changing_field, 0)) > 0)
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
        parsed_frame_evidence: list[dict[str, Any]] = []
        legacy_identifier_evidence: dict[tuple[int, bool], dict[str, Any]] = {}
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
                parsed_frame_evidence.append(frame)
                if artifact_schema_version == 1:
                    _update_identifier_evidence(legacy_identifier_evidence, frame)
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
            expected_identifiers = (
                aggregate_can_identifiers(parsed_frame_evidence)
                if artifact_schema_version == 2
                else _summarize_identifier_evidence(legacy_identifier_evidence)
            )
            if all_identifiers != expected_identifiers:
                raise ValueError("CAN identifier summary does not match frame evidence")
            if artifact_schema_version == 2 and (
                parsed_frame_evidence != source_decoded["frames"]
                or identifiers_bytes.decode("utf-8")
                != CanDecodeManager._render_identifiers_csv(expected_identifiers)
            ):
                raise ValueError("CAN child artifacts do not match decoded source evidence")
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
        identifiers = (
            filtered_identifiers
            if identifier_limit is None
            else filtered_identifiers[:identifier_limit]
        )
        total_identifiers = len(filtered_identifiers)
        timeline = build_can_timeline(frames, limit=200)
        if artifact_schema_version == 2:
            physical_layer_diagnostics = manifest["physical_layer_diagnostics"]
            integrity_diagnostics = manifest["integrity_diagnostics"]
            capabilities = integrity_diagnostics["capabilities"]
            capture_duration_us = physical_layer_diagnostics["capture_duration_us"]
            heatmap = build_can_payload_heatmap(
                all_identifiers,
                capture_duration_us=float(capture_duration_us),
                identifier_filter=identifier_filter,
                changing_only=changing_only,
                identifier_limit=200,
            )
        else:
            physical_layer_diagnostics = None
            integrity_diagnostics = None
            capture_duration_us = None
            capabilities = {
                "legacy_evidence": True,
                "sampled_waveform_analysis_available": False,
                "scope_acquisition_available": True,
                "long_listen_only_can_adapter_available": False,
                "socketcan_or_provider_available": False,
                "transmit_available": False,
                "replay_available": False,
                "query_available": False,
            }
            heatmap = {
                "source_identifier_count": len(all_identifiers),
                "total_identifier_count": 0,
                "returned_identifier_count": 0,
                "identifiers_truncated": False,
                "identifier_limit": 200,
                "bin_count": 64,
                "bin_width_bits": 1,
                "cell_limit": 12_800,
                "returned_cell_count": 0,
                "capture_interval_us": {"start": 0.0, "end": capture_duration_us},
                "ranking_policy": "unavailable for legacy artifact schema v1",
                "selection_policy": "unavailable for legacy artifact schema v1",
                "cell_semantics": "unavailable for legacy artifact schema v1",
                "identifiers": [],
            }
        return {
            "artifact_schema_version": artifact_schema_version,
            "decoder_algorithm_version": manifest.get("decoder_algorithm_version", 1),
            "analyzer_version": manifest.get("analyzer_version"),
            "decoder_settings": manifest.get("decoder_settings", {"classical_can_only": True}),
            "run_id": run_id,
            "capture_id": record["id"],
            "captured_at": manifest.get("captured_at"),
            "source_run_id": manifest.get("source_run_id"),
            "source_capture_id": manifest.get("source_capture_id"),
            "source_sha256": manifest.get("source_sha256"),
            "source_manifest_sha256": manifest.get("source_manifest_sha256"),
            "source_captured_at": manifest.get("source_captured_at"),
            "can_polarity": manifest.get("can_polarity"),
            "nominal_bitrate_bps": manifest.get("nominal_bitrate_bps"),
            "writes_performed": 0,
            "capture_duration_us": capture_duration_us,
            "physical_layer_diagnostics": physical_layer_diagnostics,
            "integrity_diagnostics": integrity_diagnostics,
            "capabilities": capabilities,
            "identifier_filter": identifier,
            "changing_only": changing_only,
            "frame_limit": 200,
            "total_frame_count": total_frames,
            "returned_frame_count": len(frames),
            "frames_truncated": total_frames > len(frames),
            "frames": frames,
            "identifier_limit": identifier_limit,
            "total_identifier_count": total_identifiers,
            "returned_identifier_count": len(identifiers),
            "identifiers_truncated": total_identifiers > len(identifiers),
            "identifiers": identifiers,
            "timeline_limit": 200,
            "returned_timeline_count": len(timeline),
            "timeline": timeline,
            "payload_heatmap": heatmap,
            "warnings": manifest.get("warnings", []),
            "limitations": manifest.get("limitations", []),
            "artifact_urls": {
                "frames_jsonl": f"/artifacts/{run_id}/frames.jsonl",
                "identifiers_csv": f"/artifacts/{run_id}/identifiers.csv",
            },
        }

    @app.get("/api/can-decodes/{run_id}")
    def can_decode_result(
        run_id: str,
        identifier: str = "",
        changing_only: bool = False,
    ) -> dict[str, object]:
        return _load_authoritative_can_decode_result(
            run_id, identifier, changing_only, identifier_limit=200
        )

    @app.get("/api/can-decode-comparisons")
    def compare_can_decodes(
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> dict[str, object]:
        for run_id in (baseline_run_id, candidate_run_id):
            if (
                not RUN_ID_PATTERN.fullmatch(run_id)
                or run_id in {".", ".."}
                or ".partial" in run_id.lower()
            ):
                raise HTTPException(status_code=422, detail="invalid CAN decode run ID")
        if baseline_run_id == candidate_run_id:
            raise HTTPException(
                status_code=422,
                detail="baseline and candidate must be different CAN decode runs",
            )
        baseline = _load_authoritative_can_decode_result(
            baseline_run_id, identifier_limit=None
        )
        candidate = _load_authoritative_can_decode_result(
            candidate_run_id, identifier_limit=None
        )
        try:
            return compare_can_decode_results(baseline, candidate)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def obd_result(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return operation()
        except OBDNotConnected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OBDInUse as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OBDTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OBDAdapterError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OBDProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/customers")
    def customers() -> list[dict[str, object]]:
        return database.list_customers()

    @app.post("/api/customers", status_code=201)
    def create_customer(payload: CustomerPayload) -> dict[str, object]:
        def optional(value: str | None) -> str | None:
            return value.strip() if value and value.strip() else None

        try:
            return {
                "id": database.create_customer(
                    name=payload.name,
                    company=optional(payload.company),
                    phone=optional(payload.phone),
                    email=optional(payload.email),
                    notes=optional(payload.notes),
                )
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/vehicles")
    def vehicles() -> list[dict[str, object]]:
        return database.list_vehicles()

    @app.post("/api/vehicles", status_code=201)
    def create_vehicle(payload: VehiclePayload) -> dict[str, object]:
        try:
            return {
                "id": database.create_vehicle(
                    display_name=payload.display_name.strip(),
                    vin=payload.vin.strip().upper() if payload.vin else None,
                    make=payload.make.strip() if payload.make else None,
                    model=payload.model.strip() if payload.model else None,
                    year=payload.year,
                    engine=payload.engine.strip() if payload.engine else None,
                    asset_tag=payload.asset_tag.strip() if payload.asset_tag else None,
                    notes=payload.notes.strip() if payload.notes else None,
                )
            }
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/diagnostic-sessions")
    def diagnostic_sessions() -> list[dict[str, object]]:
        return database.list_diagnostic_sessions()

    @app.post("/api/diagnostic-sessions", status_code=201)
    def create_diagnostic_session(payload: DiagnosticSessionPayload) -> dict[str, int]:
        try:
            return database.create_diagnostic_session(
                customer_id=payload.customer_id,
                vehicle_id=payload.vehicle_id,
                title=payload.title.strip(),
                purpose=payload.purpose.strip(),
                complaint=payload.complaint.strip() if payload.complaint else None,
                location=payload.location.strip() if payload.location else None,
                operator_name=(
                    payload.operator_name.strip() if payload.operator_name else None
                ),
                notes=payload.notes.strip() if payload.notes else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/diagnostic-sessions/{session_id}/obd-snapshots")
    def obd_session_snapshots(session_id: int) -> list[dict[str, object]]:
        return database.list_obd_snapshots(session_id)

    @app.get("/api/obd/status")
    def obd_status() -> dict[str, object]:
        return obd_service.status()

    @app.post("/api/obd/connect", status_code=201)
    def obd_connect(payload: OBDConnectPayload) -> dict[str, object]:
        with usb_routing_obd_lock:
            return obd_result(
                lambda: obd_service.connect(mode=payload.mode, session_id=payload.session_id)
            )

    @app.post("/api/obd/disconnect")
    def obd_disconnect() -> dict[str, object]:
        return obd_result(obd_service.disconnect)

    @app.get("/api/obd/live")
    def obd_live() -> dict[str, object]:
        return obd_result(obd_service.read_live)

    @app.get("/api/obd/faults")
    def obd_faults() -> dict[str, object]:
        return obd_result(obd_service.read_faults)

    @app.get("/api/obd/vehicle-info")
    def obd_vehicle_info() -> dict[str, object]:
        return obd_result(obd_service.read_vehicle_info)

    @app.post("/api/obd/snapshots", status_code=201)
    def create_obd_snapshot(payload: OBDSnapshotPayload) -> dict[str, object]:
        return obd_result(
            lambda: obd_evidence.save(
                kind=payload.kind,
                label=payload.label.strip(),
                operation_id=str(payload.operation_id),
            )
        )

    @app.post("/api/obd/faults/clear/prepare")
    def prepare_obd_clear() -> dict[str, object]:
        return obd_result(lambda: obd_service.clear_faults())  # type: ignore[func-returns-value]

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
            with usb_routing_obd_lock:
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
        with usb_routing_obd_lock:
            obd_status = obd_service.status()
            if obd_status["connected"] and obd_status["provider"] != "obd-simulator":
                raise HTTPException(
                    status_code=409,
                    detail="a local hardware OBD connection owns the adapter",
                )
            if pico_lock.locked():
                raise HTTPException(status_code=409, detail="a local scope or bus capture is active")
            manager = app.state.serial_hardware_manager
            if manager is not None and manager._lock.locked():
                raise HTTPException(status_code=409, detail="a local SEL serial capture is active")
            try:
                return app.state.routing_client.request({"action": "apply", "inventory_revision": payload.inventory_revision, "routes": payload.routes})
            except (OSError, RoutingSocketError) as exc:
                raise HTTPException(status_code=503, detail=f"USB routing apply failed: {exc}") from exc

    def loadbank_call(operation: Callable[[LoadBankClient], object]) -> object:
        client = app.state.loadbank_client
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="Load Bank unavailable: collector is not configured",
            )
        try:
            return operation(client)
        except CollectorUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CollectorHttpError as exc:
            status_code = exc.status_code if 400 <= exc.status_code <= 599 else 502
            raise HTTPException(status_code=status_code, detail=exc.detail) from exc

    @app.get("/api/loadbank/status")
    def loadbank_status() -> object:
        return loadbank_call(lambda client: client.status())

    @app.post("/api/loadbank/discovery")
    def loadbank_discovery(
        _payload: EmptyLoadBankPayload = Body(default_factory=EmptyLoadBankPayload),
    ) -> object:
        return loadbank_call(lambda client: client.discover())

    @app.put("/api/loadbank/ownership")
    def set_loadbank_ownership(payload: LoadBankOwnershipPayload) -> object:
        return loadbank_call(
            lambda client: client.set_ownership(
                payload.owner,
                confirmed_external_stopped=payload.confirmed_external_stopped,
            )
        )

    @app.post("/api/loadbank/sessions", status_code=201)
    def start_loadbank_session(payload: LoadBankSessionPayload) -> object:
        return loadbank_call(
            lambda client: client.start_session(
                payload.candidate_id,
                payload.duration_minutes,
                payload.metadata.model_dump(),
            )
        )

    @app.post("/api/loadbank/sessions/active/stop")
    def stop_loadbank_session(
        _payload: EmptyLoadBankPayload = Body(default_factory=EmptyLoadBankPayload),
    ) -> object:
        return loadbank_call(lambda client: client.stop_session())

    @app.get("/api/loadbank/sessions/{session_uuid}/download")
    def download_loadbank_session(session_uuid: UUID) -> Response:
        payload = loadbank_call(lambda client: client.download_session(session_uuid))
        if not isinstance(payload, bytes):
            raise HTTPException(status_code=502, detail="collector returned an invalid ZIP response")
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="load-bank-{session_uuid}.zip"'
                )
            },
        )

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
            with usb_routing_obd_lock:
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
                with usb_routing_obd_lock:
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
        create_app(
            data_dir=data_dir,
            db_path=db_path,
            loadbank_client=load_client_from_environment(),
        ),
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
