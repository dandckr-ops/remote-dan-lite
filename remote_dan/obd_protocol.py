from __future__ import annotations

import re
from typing import Any, Literal


class OBDProtocolError(ValueError):
    """Raised when an adapter response cannot be defended as valid OBD data."""


class OBDAdapterError(OBDProtocolError):
    """Raised for terminal ELM/STN transport or bus failures."""


DTC_DESCRIPTIONS = {
    "P0028": "Intake valve control solenoid circuit range/performance, bank 2",
    "P0102": "Mass or volume air flow circuit low input",
    "P0113": "Intake air temperature sensor 1 circuit high input",
}


PID_DEFINITIONS: dict[int, tuple[str, str, int]] = {
    0x04: ("Calculated engine load", "%", 1),
    0x05: ("Engine coolant temperature", "°C", 1),
    0x06: ("Short term fuel trim bank 1", "%", 1),
    0x07: ("Long term fuel trim bank 1", "%", 1),
    0x08: ("Short term fuel trim bank 2", "%", 1),
    0x09: ("Long term fuel trim bank 2", "%", 1),
    0x0B: ("Intake manifold pressure", "kPa", 1),
    0x0C: ("Engine speed", "rpm", 2),
    0x0D: ("Vehicle speed", "km/h", 1),
    0x0E: ("Timing advance", "°", 1),
    0x0F: ("Intake air temperature", "°C", 1),
    0x10: ("Mass air flow", "g/s", 2),
    0x11: ("Throttle position", "%", 1),
    0x1F: ("Engine run time", "s", 2),
    0x2F: ("Fuel level", "%", 1),
    0x42: ("Control module voltage", "V", 2),
}


def _hex_tokens(line: str) -> tuple[str, list[int]] | None:
    tokens = line.strip().replace(">", "").split()
    if len(tokens) < 2 or len(tokens[0]) not in {3, 8}:
        return None
    if not re.fullmatch(r"[0-9A-Fa-f]{3}|[0-9A-Fa-f]{8}", tokens[0]):
        raise OBDProtocolError(f"malformed ELM frame header: {tokens[0]}")
    if any(not re.fullmatch(r"[0-9A-Fa-f]{2}", token) for token in tokens[1:]):
        raise OBDProtocolError(f"malformed ELM frame data: {line.strip()}")
    return tokens[0].upper(), [int(token, 16) for token in tokens[1:]]


def _parse_elm_response(
    raw: str,
    *,
    scoped_errors: bool,
) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    payloads: dict[str, bytes] = {}
    pending: dict[str, tuple[int, bytearray, int]] = {}
    errors: list[dict[str, str]] = []
    invalid_responders: set[str] = set()
    saw_no_data = False
    terminal_errors = (
        "BUS ERROR", "CAN ERROR", "STOPPED", "UNABLE TO CONNECT",
        "BUFFER FULL", "DATA ERROR", "RX ERROR", "LV RESET",
    )

    def responder_error(ecu: str, message: str) -> None:
        if not scoped_errors:
            raise OBDProtocolError(message)
        payloads.pop(ecu, None)
        pending.pop(ecu, None)
        invalid_responders.add(ecu)
        errors.append({"ecu": ecu, "error": message})

    for line in raw.replace("\r", "\n").splitlines():
        cleaned = line.replace(">", "").strip()
        if not cleaned:
            continue
        upper = cleaned.upper()
        if upper == "NO DATA":
            saw_no_data = True
            continue
        explicit_adapter_error = (
            upper in {"?", "ERROR", "BUS BUSY"}
            or upper.endswith(" ERROR")
            or upper.endswith(" ALERT")
            or re.fullmatch(r"ERR[0-9A-F]+", upper) is not None
        )
        if explicit_adapter_error or any(error in upper for error in terminal_errors):
            raise OBDAdapterError(f"adapter reported: {cleaned}")
        if upper == "OK" or upper.startswith("SEARCHING") or upper.startswith("BUS INIT"):
            continue
        parsed = _hex_tokens(cleaned)
        if parsed is None:
            if re.fullmatch(r"(?:0[0-9A-F]{1,5}|ATRV)", upper):
                continue
            raise OBDProtocolError(f"malformed ELM response line: {cleaned}")
        ecu, data = parsed
        if not data or ecu in invalid_responders:
            continue
        frame_type = data[0] >> 4
        if frame_type == 0:
            length = data[0] & 0x0F
            if length > len(data) - 1:
                responder_error(ecu, f"single-frame length exceeds data for ECU {ecu}")
                continue
            payloads[ecu] = bytes(data[1:1 + length])
        elif frame_type == 1:
            if len(data) < 2:
                responder_error(ecu, f"truncated first frame for ECU {ecu}")
                continue
            length = ((data[0] & 0x0F) << 8) | data[1]
            buffer = bytearray(data[2:])
            if len(buffer) >= length:
                payloads[ecu] = bytes(buffer[:length])
            else:
                pending[ecu] = (length, buffer, 1)
        elif frame_type == 2:
            if ecu not in pending:
                responder_error(ecu, f"continuation without first frame for ECU {ecu}")
                continue
            length, buffer, expected_sequence = pending[ecu]
            sequence = data[0] & 0x0F
            if sequence != expected_sequence:
                responder_error(
                    ecu,
                    f"ISO-TP sequence gap for ECU {ecu}: expected {expected_sequence:X}, got {sequence:X}",
                )
                continue
            buffer.extend(data[1:])
            if len(buffer) >= length:
                payloads[ecu] = bytes(buffer[:length])
                del pending[ecu]
            else:
                pending[ecu] = (length, buffer, (expected_sequence + 1) & 0x0F)
        elif frame_type == 3:
            continue
        else:
            responder_error(ecu, f"unsupported ISO-TP frame type {frame_type:X}")
    if pending:
        if not scoped_errors:
            raise OBDProtocolError(
                "incomplete ISO-TP response from " + ", ".join(sorted(pending))
            )
        for ecu in sorted(pending):
            responder_error(ecu, f"incomplete ISO-TP response from {ecu}")
    if saw_no_data and not payloads:
        return {}, errors
    return payloads, errors


def parse_elm_response(raw: str) -> dict[str, bytes]:
    """Strictly parse header-enabled ELM/STN output and reassemble ISO-TP."""
    payloads, _ = _parse_elm_response(raw, scoped_errors=False)
    return payloads


def parse_elm_response_scoped(
    raw: str,
) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    """Parse valid responders while returning responder-local framing errors."""
    return _parse_elm_response(raw, scoped_errors=True)


def decode_supported_pids(payload: bytes) -> set[int]:
    if len(payload) < 6 or payload[0] != 0x41 or payload[1] % 0x20 != 0:
        raise OBDProtocolError("invalid Mode 01 supported-PID response")
    base = payload[1]
    bitmap = int.from_bytes(payload[2:6], "big")
    return {
        base + bit
        for bit in range(1, 33)
        if bitmap & (1 << (32 - bit))
    }


def _require_mode_01(payload: bytes, length: int) -> tuple[int, bytes]:
    if len(payload) < 2 or payload[0] != 0x41:
        raise OBDProtocolError("not a positive Mode 01 response")
    pid = payload[1]
    data = payload[2:]
    if len(data) < length:
        raise OBDProtocolError(f"PID {pid:02X} response is truncated")
    return pid, data


def decode_live_pid(
    payload: bytes,
    *,
    ecu: str,
    sampled_at: str,
) -> dict[str, Any]:
    if len(payload) < 2:
        raise OBDProtocolError("truncated live-data response")
    pid = payload[1]
    definition = PID_DEFINITIONS.get(pid)
    if definition is None:
        raise OBDProtocolError(f"PID {pid:02X} is not in the live-data catalog")
    name, unit, required = definition
    _, data = _require_mode_01(payload, required)
    a = data[0]
    b = data[1] if required > 1 else 0
    if pid in {0x04, 0x11, 0x2F}:
        value = a * 100.0 / 255.0
    elif pid in {0x05, 0x0F}:
        value = float(a - 40)
    elif pid in {0x06, 0x07, 0x08, 0x09}:
        value = (a - 128) * 100.0 / 128.0
    elif pid == 0x0B:
        value = float(a)
    elif pid == 0x0C:
        value = ((a << 8) | b) / 4.0
    elif pid == 0x0D:
        value = float(a)
    elif pid == 0x0E:
        value = a / 2.0 - 64.0
    elif pid == 0x10:
        value = ((a << 8) | b) / 100.0
    elif pid == 0x1F:
        value = float((a << 8) | b)
    elif pid == 0x42:
        value = ((a << 8) | b) / 1000.0
    else:  # pragma: no cover - guarded by PID_DEFINITIONS and branches above
        raise OBDProtocolError(f"PID {pid:02X} has no decoder")
    return {
        "service": "01",
        "pid": f"{pid:02X}",
        "name": name,
        "value": value,
        "unit": unit,
        "supported": True,
        "fresh": True,
        "sampled_at": sampled_at,
        "ecu": ecu.upper(),
        "raw_hex": data[:required].hex().upper(),
        "error": None,
    }


def decode_readiness(payload: bytes, *, ecu: str) -> dict[str, Any]:
    _, data = _require_mode_01(payload, 4)
    if payload[1] != 0x01:
        raise OBDProtocolError("not a Mode 01 PID 01 readiness response")
    a, b, c, d = data[:4]
    supported: list[str] = []
    incomplete: list[str] = []
    continuous = ["Misfire", "Fuel system", "Comprehensive components"]
    for bit, name in enumerate(continuous):
        if b & (1 << bit):
            supported.append(name)
            if b & (1 << (bit + 4)):
                incomplete.append(name)
    compression = bool(b & 0x08)
    monitor_names = (
        [
            "NMHC catalyst", "NOx/SCR", None, "Boost pressure", None,
            "Exhaust gas sensor", "Particulate matter filter", "EGR/VVT",
        ]
        if compression
        else [
            "Catalyst", "Heated catalyst", "Evaporative system",
            "Secondary air system", "A/C refrigerant", "Oxygen sensor",
            "Oxygen sensor heater", "EGR/VVT",
        ]
    )
    for bit, name in enumerate(monitor_names):
        if name is not None and c & (1 << bit):
            supported.append(name)
            if d & (1 << bit):
                incomplete.append(name)
    return {
        "ecu": ecu.upper(),
        "mil_on": bool(a & 0x80),
        "dtc_count": a & 0x7F,
        "ignition_type": "compression" if compression else "spark",
        "supported": supported,
        "incomplete": incomplete,
        "complete": [name for name in supported if name not in incomplete],
        "raw_hex": data[:4].hex().upper(),
    }


def _decode_dtc(pair: bytes) -> str:
    first, second = pair
    system = "PCBU"[(first >> 6) & 0x03]
    return f"{system}{(first >> 4) & 0x03}{first & 0x0F:X}{second >> 4:X}{second & 0x0F:X}"


def decode_dtc_payload(
    payload: bytes,
    *,
    state: Literal["stored", "pending", "permanent"],
    ecu: str,
) -> list[dict[str, Any]]:
    expected_service = {"stored": 0x43, "pending": 0x47, "permanent": 0x4A}[state]
    if not payload or payload[0] != expected_service:
        raise OBDProtocolError(f"not a positive {state} DTC response")
    data = payload[1:]
    if len(data) % 2:
        count = data[0]
        counted_data = data[1:]
        if len(counted_data) < count * 2:
            raise OBDProtocolError("count-prefixed DTC response is truncated")
        trailing = counted_data[count * 2:]
        if any(trailing):
            raise OBDProtocolError("count-prefixed DTC response has nonzero trailing data")
        data = counted_data[:count * 2]
    results: list[dict[str, Any]] = []
    for offset in range(0, len(data), 2):
        pair = data[offset:offset + 2]
        if pair == b"\x00\x00":
            continue
        code = _decode_dtc(pair)
        results.append(
            {
                "code": code,
                "state": state,
                "ecu": ecu.upper(),
                "description": DTC_DESCRIPTIONS.get(code, "Generic SAE emissions DTC"),
                "raw_hex": pair.hex().upper(),
            }
        )
    return results


def decode_vin(payload: bytes) -> str:
    if len(payload) < 3 or payload[:2] != bytes((0x49, 0x02)):
        raise OBDProtocolError("not a positive Mode 09 VIN response")
    vin_bytes = payload[3:].rstrip(b"\x00")
    try:
        vin = vin_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OBDProtocolError("VIN contains non-ASCII bytes") from exc
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise OBDProtocolError("VIN is not a valid 17-character identifier")
    return vin
