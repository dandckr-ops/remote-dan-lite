from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from remote_dan.obd_provider import (
    ELMSerialProvider,
    OBDNotConnected,
    OBDTimeout,
    SimulatorOBDProvider,
)
from remote_dan.obd_protocol import parse_elm_response


class FakeSerial:
    def __init__(self, responses: dict[str, bytes], **kwargs: Any) -> None:
        self.responses = responses
        self.kwargs = kwargs
        self.writes: list[str] = []
        self.pending = bytearray()
        self.closed = False

    def reset_input_buffer(self) -> None:
        self.pending.clear()

    def write(self, data: bytes) -> int:
        command = data.decode("ascii").rstrip("\r").upper()
        self.writes.append(command)
        self.pending.extend(self.responses.get(command, b"OK\r>"))
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        if not self.pending:
            return b""
        chunk = bytes(self.pending[:size])
        del self.pending[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeSerialFactory:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.instances: list[FakeSerial] = []

    def __call__(self, **kwargs: Any) -> FakeSerial:
        instance = FakeSerial(self.responses, **kwargs)
        self.instances.append(instance)
        return instance


def test_simulator_provider_requires_explicit_connection() -> None:
    provider = SimulatorOBDProvider()

    with pytest.raises(OBDNotConnected):
        provider.query("0100")

    identity = provider.connect()
    response = provider.query("0100")
    provider.disconnect()

    assert identity["provider"] == "obd-simulator"
    assert identity["protocol"] == "ISO 15765-4 CAN 11/500 (simulated)"
    assert parse_elm_response(response)["7E8"].startswith(bytes.fromhex("41 00"))
    with pytest.raises(OBDNotConnected):
        provider.query("0100")


def test_simulator_clear_is_available_only_when_explicitly_enabled() -> None:
    blocked = SimulatorOBDProvider()
    blocked.connect()
    with pytest.raises(PermissionError, match="disabled"):
        blocked.query("04")

    enabled = SimulatorOBDProvider(allow_clear=True)
    enabled.connect()
    assert parse_elm_response(enabled.query("03"))["7E8"][1] == 3

    clear_response = enabled.query("04")

    assert parse_elm_response(clear_response)["7E8"] == bytes.fromhex("44")
    assert parse_elm_response(enabled.query("03"))["7E8"] == bytes.fromhex("43 00")


def test_elm_serial_provider_uses_stable_path_exclusive_open_and_bounded_init(
    tmp_path: Path,
) -> None:
    responses = {
        "": b">",
        "ATZ": b"ELM327 v1.3a\r>",
        "ATI": b"OBDLink SX r4.2\r>",
        "STI": b"STN1130 v4.0.1\r>",
        "AT@1": b"SCANTOOL.NET LLC\r>",
        "0100": b"7E8 06 41 00 BE 3F A8 13\r>",
        "ATDP": b"ISO 15765-4 (CAN 11/500)\r>",
        "ATDPN": b"A6\r>",
        "ATRV": b"13.9V\r>",
        "010C": b"7E8 04 41 0C 1A F8 00 00\r>",
        "ATPC": b"OK\r>",
    }
    factory = FakeSerialFactory(responses)
    stable_path = "/dev/serial/by-id/usb-ScanTool.net_LLC_OBDLink_SX_test"
    provider = ELMSerialProvider(
        stable_path=stable_path,
        serial_factory=factory,
        lock_path=tmp_path / "obdlink.lock",
    )

    identity = provider.connect()
    rpm = provider.query("010C")
    provider.disconnect()

    serial = factory.instances[0]
    assert serial.kwargs["port"] == stable_path
    assert serial.kwargs["baudrate"] == 115200
    assert serial.kwargs["exclusive"] is True
    assert serial.closed is True
    assert identity["adapter_identity"] == "OBDLink SX r4.2 / STN1130 v4.0.1"
    assert identity["voltage"] == 13.9
    assert parse_elm_response(rpm)["7E8"] == bytes.fromhex("41 0C 1A F8")
    assert serial.writes[:5] == ["", "ATZ", "ATE0", "ATL0", "ATS1"]
    assert "ATCAF1" in serial.writes
    assert "ATCFC1" in serial.writes
    assert serial.writes[-1] == "ATPC"


def test_elm_timeout_is_not_automatically_retried(tmp_path: Path) -> None:
    responses = {
        "": b">",
        "ATZ": b"ELM327 v1.3a\r>",
        "ATI": b"OBDLink SX r4.2\r>",
        "STI": b"STN1130 v4.0.1\r>",
        "AT@1": b"SCANTOOL.NET LLC\r>",
        "0100": b"7E8 06 41 00 BE 3F A8 13\r>",
        "ATDP": b"ISO 15765-4 (CAN 11/500)\r>",
        "ATDPN": b"A6\r>",
        "ATRV": b"13.9V\r>",
        "03": b"7E8 10 08 43 03",
        "ATPC": b"OK\r>",
    }
    factory = FakeSerialFactory(responses)
    provider = ELMSerialProvider(
        stable_path="/dev/serial/by-id/obdlink-test",
        serial_factory=factory,
        lock_path=tmp_path / "obdlink.lock",
        command_timeout_s=0.01,
        reset_timeout_s=0.05,
    )
    provider.connect()

    with pytest.raises(OBDTimeout, match="03"):
        provider.query("03")

    assert Counter(factory.instances[0].writes)["03"] == 1
    provider.disconnect()
