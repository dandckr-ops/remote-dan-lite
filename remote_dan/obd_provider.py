from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from remote_dan.obd_protocol import OBDProtocolError, parse_elm_response


class OBDProviderError(RuntimeError):
    pass


class OBDNotConnected(OBDProviderError):
    pass


class OBDInUse(OBDProviderError):
    pass


class OBDTimeout(OBDProviderError):
    pass


class OBDProvider(Protocol):
    name: str

    def connect(self) -> dict[str, Any]: ...

    def disconnect(self) -> None: ...

    def query(self, command: str) -> str: ...


_ALLOWED_QUERY = re.compile(
    r"^(?:01[0-9A-F]{2}|02[0-9A-F]{4}|03|04|07|09[0-9A-F]{2}|0A|ATRV)$"
)


def _canonical_command(command: str) -> str:
    value = "".join(command.upper().split())
    if not _ALLOWED_QUERY.fullmatch(value):
        raise ValueError("command is outside the bounded generic OBD allowlist")
    return value


def _response_text(raw: str, command: str) -> str:
    ignored = {
        command.upper(), "OK", "SEARCHING...", "BUS INIT...",
    }
    lines = [
        line.strip()
        for line in raw.replace(">", "").replace("\r", "\n").splitlines()
    ]
    return " / ".join(line for line in lines if line and line.upper() not in ignored)


class SimulatorOBDProvider:
    name = "obd-simulator"

    def __init__(self, *, allow_clear: bool = False) -> None:
        self.allow_clear = allow_clear
        self.connected = False
        self.cleared = False
        self.generation: str | None = None

    def connect(self) -> dict[str, Any]:
        if self.connected:
            raise OBDInUse("OBD simulator is already connected")
        self.connected = True
        self.cleared = False
        self.generation = uuid4().hex
        return {
            "provider": self.name,
            "adapter_identity": "OBDLink SX simulator / STN1130 fixture",
            "stable_path": None,
            "protocol": "ISO 15765-4 CAN 11/500 (simulated)",
            "responder_ids": ["7E8"],
            "voltage": 13.8,
            "connection_generation": self.generation,
        }

    def disconnect(self) -> None:
        self.connected = False
        self.generation = None

    def query(self, command: str) -> str:
        if not self.connected:
            raise OBDNotConnected("OBD simulator is not connected")
        command = _canonical_command(command)
        if command == "04":
            if not self.allow_clear:
                raise PermissionError("simulated fault clearing is disabled")
            self.cleared = True
            return "7E8 01 44 00 00 00 00 00 00\r>"
        if command == "03":
            if self.cleared:
                return "7E8 02 43 00 00 00 00 00 00\r>"
            return (
                "7E8 10 08 43 03 01 02 01 13\r"
                "7E8 21 00 28 00 00 00 00 00\r>"
            )
        responses = {
            "0100": "7E8 06 41 00 BE 3F A8 13\r>",
            "0120": "7E8 06 41 20 90 1F F0 11\r>",
            "0140": "7E8 06 41 40 78 DC 80 00\r>",
            "0101": "7E8 06 41 01 03 07 E5 00\r>",
            "0104": "7E8 03 41 04 40 00 00 00\r>",
            "0105": "7E8 03 41 05 5A 00 00 00\r>",
            "0106": "7E8 03 41 06 80 00 00 00\r>",
            "0107": "7E8 03 41 07 82 00 00 00\r>",
            "010B": "7E8 03 41 0B 64 00 00 00\r>",
            "010C": "7E8 04 41 0C 1A F8 00 00\r>",
            "010D": "7E8 03 41 0D 37 00 00 00\r>",
            "010E": "7E8 03 41 0E 90 00 00 00\r>",
            "010F": "7E8 03 41 0F 50 00 00 00\r>",
            "0110": "7E8 04 41 10 01 7C 00 00\r>",
            "0111": "7E8 03 41 11 2C 00 00 00\r>",
            "011F": "7E8 04 41 1F 00 78 00 00\r>",
            "012F": "7E8 03 41 2F 80 00 00 00\r>",
            "0142": "7E8 04 41 42 36 B0 00 00\r>",
            "07": "7E8 02 47 00 00 00 00 00 00\r>",
            "0A": "NO DATA\r>",
            "0900": "7E8 06 49 00 55 00 00 00\r>",
            "0902": (
                "7E8 10 14 49 02 01 52 44\r"
                "7E8 21 4C 54 45 53 54 31 32\r"
                "7E8 22 33 34 35 36 37 38 39\r"
                "7E8 23 30 00 00 00 00 00 00\r>"
            ),
            "ATRV": "13.8V\r>",
        }
        return responses.get(command, "NO DATA\r>")


class ELMSerialProvider:
    name = "obdlink-sx"

    def __init__(
        self,
        *,
        stable_path: str | None = None,
        serial_factory: Callable[..., Any] | None = None,
        lock_path: Path | str | None = None,
        command_timeout_s: float = 1.2,
        reset_timeout_s: float = 3.0,
    ) -> None:
        self.stable_path = (
            stable_path or os.environ.get("REMOTE_DAN_OBD_DEVICE", "")
        ).strip()
        self.serial_factory = serial_factory
        self.lock_path = Path(
            lock_path
            or os.environ.get(
                "REMOTE_DAN_OBD_LOCK_PATH",
                "/var/lib/remote-dan-lite/obdlink-sx.lock",
            )
        )
        self.command_timeout_s = command_timeout_s
        self.reset_timeout_s = reset_timeout_s
        self._serial: Any | None = None
        self._lock_fd: int | None = None
        self._identity: dict[str, Any] | None = None

    def _acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise OBDInUse("OBDLink SX is owned by another process") from exc
        self._lock_fd = descriptor

    def _open_serial(self) -> Any:
        factory = self.serial_factory
        if factory is None:
            import serial

            factory = serial.Serial
        return factory(
            port=self.stable_path,
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.05,
            write_timeout=1.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            exclusive=True,
        )

    def _exchange(self, command: str, *, timeout_s: float) -> str:
        if self._serial is None:
            raise OBDNotConnected("OBDLink SX is not connected")
        self._serial.reset_input_buffer()
        wire = f"{command}\r".encode("ascii")
        self._serial.write(wire)
        self._serial.flush()
        deadline = time.monotonic() + timeout_s
        received = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(256)
            if chunk:
                received.extend(chunk)
                if b">" in received:
                    return received.decode("ascii", errors="replace")
            else:
                time.sleep(0.002)
        label = command or "prompt synchronization"
        raise OBDTimeout(f"adapter command {label} timed out")

    def connect(self) -> dict[str, Any]:
        if self._serial is not None:
            raise OBDInUse("OBDLink SX is already connected")
        if not self.stable_path:
            raise OBDProviderError(
                "hardware OBD is not configured; set REMOTE_DAN_OBD_DEVICE "
                "to the adapter's /dev/serial/by-id path"
            )
        self._acquire_lock()
        try:
            self._serial = self._open_serial()
            self._exchange("", timeout_s=self.command_timeout_s)
            self._exchange("ATZ", timeout_s=self.reset_timeout_s)
            for command in (
                "ATE0", "ATL0", "ATS1", "ATH1", "ATAL", "ATCAF1",
                "ATCFC1", "ATAT1", "ATST64", "ATSP6", "ATSH7DF",
            ):
                response = self._exchange(command, timeout_s=self.command_timeout_s)
                if "?" in response or "ERROR" in response.upper():
                    raise OBDProviderError(f"adapter rejected initialization command {command}")
            ati = self._exchange("ATI", timeout_s=self.command_timeout_s)
            sti = self._exchange("STI", timeout_s=self.command_timeout_s)
            stdi = self._exchange("STDI", timeout_s=self.command_timeout_s)
            self._exchange("AT@1", timeout_s=self.command_timeout_s)
            proof_raw = self._exchange("0100", timeout_s=self.command_timeout_s)
            proof = parse_elm_response(proof_raw)
            if not proof:
                raise OBDProviderError("vehicle did not answer Mode 01 PID 00")
            protocol_text = _response_text(
                self._exchange("ATDP", timeout_s=self.command_timeout_s), "ATDP"
            )
            protocol_number = _response_text(
                self._exchange("ATDPN", timeout_s=self.command_timeout_s), "ATDPN"
            )
            if not protocol_number.endswith("6"):
                raise OBDProviderError(
                    f"adapter selected unexpected protocol {protocol_number or 'unknown'}"
                )
            voltage_text = _response_text(
                self._exchange("ATRV", timeout_s=self.command_timeout_s), "ATRV"
            )
            voltage_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*V", voltage_text, re.I)
            model_identity = _response_text(stdi, "STDI")
            if not model_identity or model_identity in {"?", "OK"}:
                model_identity = _response_text(ati, "ATI")
            identity = {
                "provider": self.name,
                "adapter_identity": (
                    f"{model_identity} / {_response_text(sti, 'STI')}"
                ),
                "stable_path": self.stable_path,
                "protocol": protocol_text,
                "responder_ids": sorted(proof),
                "voltage": float(voltage_match.group(1)) if voltage_match else None,
                "connection_generation": uuid4().hex,
            }
            self._identity = identity
            return dict(identity)
        except Exception as primary_error:
            try:
                self._release()
            except Exception as cleanup_error:
                primary_error.add_note(f"OBD cleanup also failed: {cleanup_error}")
            raise

    def query(self, command: str) -> str:
        if self._serial is None:
            raise OBDNotConnected("OBDLink SX is not connected")
        canonical = _canonical_command(command)
        if canonical == "04":
            raise PermissionError("hardware fault clearing is disabled")
        return self._exchange(canonical, timeout_s=self.command_timeout_s)

    def disconnect(self) -> None:
        command_error: Exception | None = None
        cleanup_error: Exception | None = None
        try:
            if self._serial is not None:
                self._exchange("ATPC", timeout_s=self.command_timeout_s)
        except Exception as exc:
            command_error = exc
        try:
            self._release()
        except Exception as exc:
            cleanup_error = exc
        if command_error is not None and cleanup_error is not None:
            raise OBDProviderError("adapter disconnect and cleanup failed") from ExceptionGroup(
                "adapter disconnect failures", [command_error, cleanup_error]
            )
        if command_error is not None:
            raise OBDProviderError("adapter disconnect failed") from command_error
        if cleanup_error is not None:
            raise OBDProviderError("adapter cleanup failed") from cleanup_error

    def _release(self) -> None:
        errors: list[Exception] = []
        serial_port, self._serial = self._serial, None
        lock_fd, self._lock_fd = self._lock_fd, None
        self._identity = None
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception as exc:
                errors.append(exc)
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception as exc:
                errors.append(exc)
            try:
                os.close(lock_fd)
            except Exception as exc:
                errors.append(exc)
        if errors:
            cause: BaseException = (
                errors[0]
                if len(errors) == 1
                else ExceptionGroup("adapter ownership cleanup failures", errors)
            )
            raise OBDProviderError("adapter ownership cleanup failed") from cause
