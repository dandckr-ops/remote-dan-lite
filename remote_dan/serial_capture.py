from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shutil

import tempfile
import termios
import threading
import time
from typing import Any, Protocol

import matplotlib.pyplot as plt

from remote_dan.database import EvidenceDatabase
from remote_dan.serial_analysis import SerialFraming, analyze_serial_capture


SEL_C662_GLOB = "usb-Silicon_Labs_SEL_USB_to_UART_Bridge_*-if00-port0"
DEFAULT_SERIAL_BY_ID = Path("/dev/serial/by-id")


def probe_serial_hardware(
    by_id_dir: Path = DEFAULT_SERIAL_BY_ID,
) -> dict[str, Any]:
    root = Path(by_id_dir)
    matches = sorted(root.glob(SEL_C662_GLOB)) if root.is_dir() else []
    if not matches:
        return {
            "device_present": False,
            "model": "SEL C662 Serial Cable",
            "stable_path": None,
            "device_path": None,
            "reason": "SEL C662 serial cable not detected",
        }
    stable = matches[0]
    target = stable.resolve()
    return {
        "device_present": target.exists(),
        "model": "SEL C662 Serial Cable",
        "stable_path": str(stable),
        "device_path": str(target),
        "reason": (
            "SEL C662 ready for receive-only capture"
            if target.exists()
            else "SEL C662 by-ID link is present but its TTY target is missing"
        ),
    }


@dataclass(frozen=True)
class SerialCaptureRequest:
    label: str
    duration_s: float
    framing: SerialFraming
    mode: str = "auto"
    session_id: int | None = None


@dataclass(frozen=True)
class SerialChunk:
    elapsed_ms: float
    data: bytes
    marked_data: bytes | None = None


@dataclass(frozen=True)
class SerialCaptureData:
    backend: str
    device: str
    duration_s: float
    framing: SerialFraming
    chunks: tuple[SerialChunk, ...]
    receiver_errors: dict[str, int] = field(default_factory=dict)

    @property
    def raw(self) -> bytes:
        return b"".join(chunk.data for chunk in self.chunks)

    @property
    def marked_raw(self) -> bytes:
        return b"".join(
            chunk.marked_data if chunk.marked_data is not None else chunk.data
            for chunk in self.chunks
        )


class SerialCaptureBackend(Protocol):
    name: str

    def capture(self, request: SerialCaptureRequest) -> SerialCaptureData: ...


class SerialSimulatorBackend:
    name = "serial-simulator"

    def capture(self, request: SerialCaptureRequest) -> SerialCaptureData:
        chunks = (
            SerialChunk(125.0, b"\r\nFID=SEL-751A-R100-V0\r\n"),
            SerialChunk(310.0, b"DEVID=FEEDER_1\r\nPARTNO=751A01\r\n"),
            SerialChunk(475.0, b"=>"),
        )
        return SerialCaptureData(
            backend=self.name,
            device="simulated SEL C662 receive lane",
            duration_s=request.duration_s,
            framing=request.framing,
            chunks=chunks,
            receiver_errors={"parity": 0, "framing": 0, "break": 0, "overrun": 0},
        )


_BAUD_CONSTANTS = {
    value: getattr(termios, f"B{value}")
    for value in (300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)
    if hasattr(termios, f"B{value}")
}


class TermiosSerialBackend:
    """Receive-only Linux TTY backend. This class intentionally has no write API."""

    name = "sel-c662"

    def __init__(self, port_path: Path) -> None:
        self.port_path = Path(port_path)

    @staticmethod
    def _configure(fd: int, framing: SerialFraming) -> None:
        try:
            speed = _BAUD_CONSTANTS[framing.baud]
        except KeyError as exc:
            raise ValueError(f"unsupported termios baud: {framing.baud}") from exc
        attrs = termios.tcgetattr(fd)
        attrs[0] = termios.INPCK | termios.PARMRK
        attrs[1] = 0
        cflag = termios.CLOCAL | termios.CREAD | speed
        cflag |= {
            5: termios.CS5,
            6: termios.CS6,
            7: termios.CS7,
            8: termios.CS8,
        }[framing.data_bits]
        if framing.parity != "N":
            cflag |= termios.PARENB
        if framing.parity == "O":
            cflag |= termios.PARODD
        if framing.stop_bits == 2:
            cflag |= termios.CSTOPB
        if hasattr(termios, "CRTSCTS"):
            cflag &= ~termios.CRTSCTS
        cflag |= termios.HUPCL
        attrs[2] = cflag
        attrs[3] = 0
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)

    @staticmethod
    def _clear_modem_outputs(fd: int) -> None:
        mask = termios.TIOCM_DTR | termios.TIOCM_RTS
        try:
            packed = array("i", [mask])
            if packed.itemsize != 4:
                raise RuntimeError("Linux modem-control ioctl requires a 32-bit C int")
            fcntl.ioctl(fd, termios.TIOCMBIC, packed, True)
        except OSError:
            pass

    @staticmethod
    def _modem_outputs_asserted(fd: int) -> bool | None:
        bits = array("i", [0])
        try:
            fcntl.ioctl(fd, termios.TIOCMGET, bits, True)
        except OSError:
            return None
        return bool(bits[0] & (termios.TIOCM_DTR | termios.TIOCM_RTS))

    @staticmethod
    def _read_error_counters(fd: int) -> dict[str, int] | None:
        values = array("i", [0] * 19)
        try:
            fcntl.ioctl(fd, getattr(termios, "TIOCGICOUNT", 0x545D), values, True)
        except OSError:
            return None
        return {
            "framing": values[6],
            "overrun": values[7],
            "parity": values[8],
            "break": values[9],
            "buffer_overrun": values[10],
        }

    def capture(self, request: SerialCaptureRequest) -> SerialCaptureData:
        if request.duration_s <= 0 or request.duration_s > 60:
            raise ValueError("serial capture duration must be greater than 0 and no more than 60 seconds")
        fd = os.open(
            self.port_path,
            os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        )
        chunks: list[SerialChunk] = []
        errors = {
            "parity": 0,
            "framing": 0,
            "break": 0,
            "overrun": 0,
            "buffer_overrun": 0,
            "marked_parity_or_framing": 0,
            "truncated_marker": 0,
        }
        pending = bytearray()
        try:
            try:
                fcntl.ioctl(fd, getattr(termios, "TIOCEXCL", 0x540C))
            except OSError:
                pass
            self._clear_modem_outputs(fd)
            self._configure(fd, request.framing)
            self._clear_modem_outputs(fd)
            if self._modem_outputs_asserted(fd) is True:
                raise RuntimeError("DTR or RTS remained logically asserted after receive setup")
            counters_before = self._read_error_counters(fd)
            started = time.monotonic()
            deadline = started + request.duration_s
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                ready, _, _ = select.select([fd], [], [], min(0.05, max(0.0, remaining)))
                if not ready:
                    continue
                try:
                    block = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not block:
                    continue
                pending.extend(block)
                decoded = bytearray()
                index = 0
                while index < len(pending):
                    if pending[index] != 0xFF:
                        decoded.append(pending[index])
                        index += 1
                        continue
                    if index + 1 >= len(pending):
                        break
                    if pending[index + 1] == 0xFF:
                        decoded.append(0xFF)
                        index += 2
                        continue
                    if index + 2 >= len(pending):
                        break
                    marked = pending[index + 2]
                    if marked == 0:
                        errors["break"] += 1
                    else:
                        errors["marked_parity_or_framing"] += 1
                        decoded.append(marked)
                    index += 3
                del pending[:index]
                chunks.append(SerialChunk(
                    (time.monotonic() - started) * 1000.0,
                    bytes(decoded),
                    marked_data=block,
                ))
            if pending:
                errors["truncated_marker"] = 1
            counters_after = self._read_error_counters(fd)
            if counters_before is not None and counters_after is not None:
                for name in ("parity", "framing", "break", "overrun", "buffer_overrun"):
                    errors[name] = max(0, counters_after[name] - counters_before[name])
        finally:
            os.close(fd)
        return SerialCaptureData(
            backend=self.name,
            device=str(self.port_path),
            duration_s=request.duration_s,
            framing=request.framing,
            chunks=tuple(chunks),
            receiver_errors=errors,
        )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "serial-capture"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ascii(data: bytes) -> str:
    return "".join(chr(value) if 32 <= value <= 126 else "." for value in data)


class SerialCaptureManager:
    def __init__(
        self,
        data_dir: Path,
        backend: SerialCaptureBackend,
        database: EvidenceDatabase | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.database = database
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def run(self, request: SerialCaptureRequest) -> dict[str, object]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a serial capture is already in progress")
        try:
            return self._run_locked(request)
        finally:
            self._lock.release()

    def _run_locked(self, request: SerialCaptureRequest) -> dict[str, object]:
        data = self.backend.capture(request)
        captured_at = datetime.now(UTC)
        run_id = (
            captured_at.strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{_slugify(request.label)}-serial"
        )
        partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=self.data_dir))
        final = self.data_dir / run_id
        capture_id: int | None = None
        try:
            analysis = analyze_serial_capture(
                data=data.raw,
                frames=[chunk.data for chunk in data.chunks],
                framing=data.framing,
                duration_s=data.duration_s,
                receiver_errors=data.receiver_errors,
                frame_boundaries_reliable=data.backend == "serial-simulator",
            )
            summary: dict[str, object] = {
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "serial capture",
                "backend": data.backend,
                "capture_type": "serial",
                "profile": "serial",
                "duration_ms": data.duration_s * 1000.0,
                "device": data.device,
                "chunk_count": len(data.chunks),
                "decoded_byte_count": len(data.raw),
                "marked_stream_byte_count": len(data.marked_raw),
                "serial_analysis": analysis,
            }
            (partial / "capture.bin").write_bytes(data.marked_raw)
            with (partial / "chunks.jsonl").open("w", encoding="utf-8") as handle:
                for chunk in data.chunks:
                    handle.write(json.dumps({
                        "elapsed_ms": chunk.elapsed_ms,
                        "length": len(chunk.data),
                        "hex": chunk.data.hex(" "),
                        "marked_hex": (
                            chunk.marked_data.hex(" ")
                            if chunk.marked_data is not None
                            else chunk.data.hex(" ")
                        ),
                    }, sort_keys=True) + "\n")
            transcript = "\n".join(
                f"{chunk.elapsed_ms:10.3f} ms  {chunk.data.hex(' '):<96}  {_ascii(chunk.data)}"
                for chunk in data.chunks
            ) or "No serial bytes received."
            (partial / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
            (partial / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self._write_plot(
                partial / "overview.png",
                partial / "report.pdf",
                request,
                data,
            )
            artifacts = [
                "capture.bin",
                "chunks.jsonl",
                "transcript.txt",
                "overview.png",
                "report.pdf",
                "summary.json",
                "manifest.json",
            ]
            sha256 = {
                name: _sha256(partial / name)
                for name in artifacts
                if name != "manifest.json"
            }
            if self.database is not None:
                capture_id = self.database.create_capture(
                    session_id=request.session_id,
                    run_id=run_id,
                    captured_at=captured_at.isoformat(),
                    capture_type="serial",
                    label=request.label.strip() or "serial capture",
                    backend=data.backend,
                    preset=f"{data.duration_s:g}s",
                    samples=len(data.raw),
                    sample_interval_us=None,
                    duration_ms=data.duration_s * 1000.0,
                    metadata={
                        "device": data.device,
                        "framing": data.framing.as_dict(),
                        "summary": summary,
                    },
                )
            manifest: dict[str, object] = {
                "run_id": run_id,
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "serial capture",
                "capture_type": "serial",
                "profile": "serial",
                "preset": f"{data.duration_s:g}s",
                "backend": data.backend,
                "samples": len(data.raw),
                "sample_interval_us": None,
                "duration_ms": data.duration_s * 1000.0,
                "channels": ["RX"],
                "artifacts": artifacts,
                "sha256": sha256,
                "summary": summary,
            }
            if capture_id is not None:
                manifest["capture_id"] = capture_id
            (partial / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            partial.rename(final)
            if self.database is not None and capture_id is not None:
                metadata = {
                    "capture.bin": ("raw_serial", "application/octet-stream"),
                    "chunks.jsonl": ("serial_timing", "application/x-ndjson"),
                    "transcript.txt": ("serial_transcript", "text/plain"),
                    "overview.png": ("preview", "image/png"),
                    "report.pdf": ("report", "application/pdf"),
                    "summary.json": ("summary", "application/json"),
                    "manifest.json": ("manifest", "application/json"),
                }
                for name in artifacts:
                    path = final / name
                    kind, media_type = metadata[name]
                    self.database.add_artifact(
                        capture_id=capture_id,
                        kind=kind,
                        filename=name,
                        relative_path=f"{run_id}/{name}",
                        media_type=media_type,
                        size_bytes=path.stat().st_size,
                        sha256=_sha256(path),
                    )
                self.database.set_capture_status(capture_id, "complete")
            return manifest
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            if self.database is not None and capture_id is not None:
                self.database.delete_capture(capture_id)
            raise

    @staticmethod
    def _write_plot(
        png_path: Path,
        pdf_path: Path,
        request: SerialCaptureRequest,
        data: SerialCaptureData,
    ) -> None:
        with plt.rc_context({
            "figure.facecolor": "#07100D",
            "axes.facecolor": "#0C1914",
            "axes.edgecolor": "#284136",
            "axes.labelcolor": "#E9EEE9",
            "xtick.color": "#8CA095",
            "ytick.color": "#8CA095",
            "text.color": "#E9EEE9",
            "grid.color": "#284136",
            "font.family": "DejaVu Sans",
        }):
            figure, axis = plt.subplots(1, 1, figsize=(12, 5.2), constrained_layout=True)
            x_values: list[float] = []
            y_values: list[int] = []
            char_ms = (1 + data.framing.data_bits + (data.framing.parity != "N") + data.framing.stop_bits) * 1000.0 / data.framing.baud
            for chunk in data.chunks:
                for index, value in enumerate(chunk.data):
                    x_values.append(chunk.elapsed_ms + index * char_ms)
                    y_values.append(value)
            if x_values:
                axis.scatter(x_values, y_values, s=12, color="#5CFF9A", alpha=0.9)
            else:
                axis.text(0.5, 0.5, "NO SERIAL BYTES RECEIVED", ha="center", va="center", transform=axis.transAxes)
            axis.set_xlabel("TIME / ms")
            axis.set_ylabel("RECEIVED BYTE / decimal")
            axis.set_ylim(-5, 260)
            axis.grid(True, alpha=0.45)
            figure.suptitle(
                f"FIELD JOURNAL · {request.label.strip() or 'SERIAL CAPTURE'} · {data.framing.label} · RX ONLY"
            )
            figure.savefig(png_path, dpi=150, metadata={"Software": "Remote Dan Lite"})
            figure.savefig(pdf_path, format="pdf", metadata={
                "Title": f"Remote Dan Lite serial capture: {request.label.strip() or 'serial capture'}",
                "Author": "Field Journal",
                "Subject": "Traceworks receive-only serial evidence",
            })
            plt.close(figure)
