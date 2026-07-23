from __future__ import annotations

from pathlib import Path
import json
import os
import pty
import threading
import time

from remote_dan.serial_analysis import SerialFraming
from remote_dan.database import EvidenceDatabase
from remote_dan.serial_capture import (
    SerialCaptureManager,
    SerialCaptureRequest,
    SerialSimulatorBackend,
    TermiosSerialBackend,
    probe_serial_hardware,
)


def test_probe_serial_hardware_resolves_sel_c662_stable_path(tmp_path: Path) -> None:
    dev = tmp_path / "dev"
    by_id = dev / "serial" / "by-id"
    by_id.mkdir(parents=True)
    tty = dev / "ttyUSB3"
    tty.write_bytes(b"")
    stable = by_id / "usb-Silicon_Labs_SEL_USB_to_UART_Bridge_TEST-if00-port0"
    stable.symlink_to(tty)

    result = probe_serial_hardware(by_id_dir=by_id)

    assert result["device_present"] is True
    assert result["model"] == "SEL C662 Serial Cable"
    assert result["stable_path"] == str(stable)
    assert result["device_path"] == str(tty)
    assert result["reason"] == "SEL C662 ready for receive-only capture"


def test_probe_serial_hardware_fails_closed_when_c662_is_absent(tmp_path: Path) -> None:
    result = probe_serial_hardware(by_id_dir=tmp_path / "missing")

    assert result["device_present"] is False
    assert result["stable_path"] is None
    assert "not detected" in result["reason"]


def test_serial_simulator_persists_raw_timing_and_analysis_evidence(tmp_path: Path) -> None:
    manager = SerialCaptureManager(tmp_path, backend=SerialSimulatorBackend())

    manifest = manager.run(
        SerialCaptureRequest(
            label="SEL terminal proof",
            duration_s=2.0,
            framing=SerialFraming(baud=9600, data_bits=8, parity="N", stop_bits=1),
            mode="simulator",
        )
    )
    run_dir = tmp_path / manifest["run_id"]

    assert manifest["backend"] == "serial-simulator"
    assert manifest["capture_type"] == "serial"
    assert manifest["profile"] == "serial"
    assert manifest["summary"]["serial_analysis"]["protocol"]["name"] == "SEL ASCII / terminal"
    assert manifest["summary"]["serial_analysis"]["framing"]["label"] == "9600 8N1"
    assert set(manifest["artifacts"]) == {
        "capture.bin",
        "chunks.jsonl",
        "transcript.txt",
        "overview.png",
        "report.pdf",
        "summary.json",
        "manifest.json",
    }
    assert b"SEL-751A" in (run_dir / "capture.bin").read_bytes()
    assert json.loads((run_dir / "chunks.jsonl").read_text().splitlines()[0])["hex"]
    for name in manifest["artifacts"]:
        assert (run_dir / name).stat().st_size > 0
    assert set(manifest["sha256"]) == set(manifest["artifacts"]) - {"manifest.json"}


def test_serial_evidence_is_registered_in_sqlite(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    manager = SerialCaptureManager(
        tmp_path / "captures",
        backend=SerialSimulatorBackend(),
        database=database,
    )

    manifest = manager.run(
        SerialCaptureRequest(
            label="serial lineage",
            duration_s=1.0,
            framing=SerialFraming(baud=9600),
        )
    )
    record = database.get_capture(manifest["capture_id"])

    assert record is not None
    assert record["status"] == "complete"
    assert record["capture_type"] == "serial"
    assert record["samples"] == manifest["samples"]
    assert len(record["artifacts"]) == 7
    assert {artifact["filename"] for artifact in record["artifacts"]} == set(manifest["artifacts"])


def test_termios_backend_receives_bytes_without_a_write_path() -> None:
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    expected = b"\r\nSEL-451\r\n=>"

    def emit() -> None:
        time.sleep(0.05)
        os.write(master_fd, expected)

    writer = threading.Thread(target=emit)
    writer.start()
    try:
        data = TermiosSerialBackend(port_path=Path(slave_path)).capture(
            SerialCaptureRequest(
                label="pty receive proof",
                duration_s=0.25,
                framing=SerialFraming(baud=9600, data_bits=8, parity="N", stop_bits=1),
                mode="hardware",
            )
        )
    finally:
        writer.join()
        os.close(master_fd)
        os.close(slave_fd)

    assert data.backend == "sel-c662"
    assert data.device == slave_path
    assert data.raw == expected
    assert data.chunks
    assert data.chunks[0].elapsed_ms >= 0
    assert data.receiver_errors == {
        "parity": 0,
        "framing": 0,
        "break": 0,
        "overrun": 0,
        "buffer_overrun": 0,
        "marked_parity_or_framing": 0,
        "truncated_marker": 0,
    }
