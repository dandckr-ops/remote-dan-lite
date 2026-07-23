from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Protocol

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from remote_dan.database import EvidenceDatabase
from remote_dan.modbus_discovery import (
    HICPDevice,
    ModbusDevice,
    scan_modbus_network,
    validate_scan_subnet,
)


@dataclass(frozen=True)
class ModbusScanRequest:
    label: str
    subnet: str
    interface: str
    connected_networks: tuple[dict[str, str], ...]
    connect_timeout_s: float = 0.3
    response_timeout_s: float = 1.25
    hicp_timeout_s: float = 1.5
    workers: int = 4
    mode: str = "auto"
    session_id: int | None = None


class ModbusScanBackend(Protocol):
    name: str

    def scan(self, request: ModbusScanRequest) -> dict[str, object]: ...


class ModbusSimulatorBackend:
    name = "modbus-simulator"

    def scan(self, request: ModbusScanRequest) -> dict[str, object]:
        network = validate_scan_subnet(
            request.subnet,
            connected_networks=request.connected_networks,
            interface=request.interface,
        )
        hosts = network.hosts()
        first = str(next(hosts))
        second = str(next(hosts, ipaddress_fallback(network)))
        native = ModbusDevice(
            ip=first,
            port=502,
            state="identity_confirmed",
            confidence="high",
            unit_id=255,
            latency_ms=8.4,
            vendor_name="Basler Electric",
            product_code="DGC-2020HD",
            revision="sim-1.0",
            exception_code=None,
        )
        gateway = HICPDevice(
            source_ip=second,
            ip=second,
            mac="00:30:11:FA:82:C5",
            protocol_version="1.30",
            fieldbus_type="ModbusGW",
            module_version="1.22.0",
            subnet_mask=str(network.netmask),
            gateway=str(network.network_address + 1),
            dhcp="OFF",
            password_protected="ON",
            hostname="anybus-sim",
        )
        devices = [native.as_dict(), gateway.as_dict()]
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "scan_type": "read_only_modbus_discovery",
            "scope": {
                "interface": request.interface,
                "subnet": str(network),
                "host_count": usable_host_count(network),
                "probed_hosts": max(usable_host_count(network) - 3, 0),
                "workers": request.workers,
                "connect_timeout_ms": request.connect_timeout_s * 1000.0,
                "response_timeout_ms": request.response_timeout_s * 1000.0,
                "hicp_timeout_ms": request.hicp_timeout_s * 1000.0,
            },
            "devices": devices,
            "transactions": [{"outcome": "simulator_no_packets"}],
            "outcome_counts": {"simulator_no_packets": 1},
            "truncated": False,
            "device_count": 2,
            "confirmed_modbus_count": 1,
            "anybus_count": 1,
            "writes_performed": 0,
            "requests_used": [
                "HMS HICP MODULE SCAN",
                "Modbus 43/14 Read Device Identification",
            ],
            "warnings": ["Simulator inventory; no packets were sent."],
            "duration_ms": 1250.0,
        }


def ipaddress_fallback(network):
    return network.network_address


def usable_host_count(network) -> int:
    if network.prefixlen >= 31:
        return network.num_addresses
    return max(network.num_addresses - 2, 0)


class LiveModbusDiscoveryBackend:
    name = "modbus-network"
    _scan_lock = threading.Lock()
    _last_scan_by_interface: dict[str, float] = {}

    def scan(self, request: ModbusScanRequest) -> dict[str, object]:
        import time

        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("a process-wide Modbus network scan is already in progress")
        try:
            now = time.monotonic()
            previous = self._last_scan_by_interface.get(request.interface)
            if previous is not None and now - previous < 60.0:
                remaining = int(60.0 - (now - previous)) + 1
                raise RuntimeError(
                    f"Modbus scan cooldown active on {request.interface}; retry in {remaining} seconds"
                )
            self._last_scan_by_interface[request.interface] = now
            return scan_modbus_network(
                request.subnet,
                connected_networks=request.connected_networks,
                interface=request.interface,
                connect_timeout=request.connect_timeout_s,
                response_timeout=request.response_timeout_s,
                hicp_timeout=request.hicp_timeout_s,
                workers=request.workers,
            )
        finally:
            self._scan_lock.release()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "modbus-discovery"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ModbusScanManager:
    def __init__(
        self,
        data_dir: Path,
        backend: ModbusScanBackend,
        database: EvidenceDatabase | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.database = database
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def run(self, request: ModbusScanRequest) -> dict[str, object]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a Modbus network scan is already in progress")
        try:
            return self._run_locked(request)
        finally:
            self._lock.release()

    def _run_locked(self, request: ModbusScanRequest) -> dict[str, object]:
        report = self.backend.scan(request)
        captured_at = datetime.now(UTC)
        run_id = (
            captured_at.strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{_slugify(request.label)}-modbus"
        )
        partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=self.data_dir))
        final = self.data_dir / run_id
        capture_id: int | None = None
        try:
            summary = {
                **report,
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "Modbus network discovery",
                "backend": self.backend.name,
                "capture_type": "modbus_scan",
                "profile": "modbus",
            }
            (partial / "scan.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (partial / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with (partial / "transactions.jsonl").open("w", encoding="utf-8") as handle:
                for transaction in summary.get("transactions", []):
                    handle.write(json.dumps(transaction, sort_keys=True) + "\n")
            self._write_devices_csv(partial / "devices.csv", summary["devices"])
            self._write_plot(
                partial / "overview.png",
                partial / "report.pdf",
                request,
                summary,
            )
            artifacts = [
                "devices.csv",
                "scan.json",
                "transactions.jsonl",
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
                    capture_type="modbus_scan",
                    label=request.label.strip() or "Modbus network discovery",
                    backend=self.backend.name,
                    preset=str(summary["scope"]["subnet"]),
                    samples=int(summary["scope"]["probed_hosts"]),
                    sample_interval_us=None,
                    duration_ms=float(summary["duration_ms"]),
                    metadata={"summary": summary},
                )
            manifest: dict[str, object] = {
                "run_id": run_id,
                "captured_at": captured_at.isoformat(),
                "label": request.label.strip() or "Modbus network discovery",
                "capture_type": "modbus_scan",
                "profile": "modbus",
                "preset": str(summary["scope"]["subnet"]),
                "backend": self.backend.name,
                "samples": int(summary["scope"]["probed_hosts"]),
                "sample_interval_us": None,
                "duration_ms": float(summary["duration_ms"]),
                "channels": ["Ethernet", "HICP UDP/3250", "Modbus TCP/502"],
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
                    "devices.csv": ("device_inventory", "text/csv"),
                    "scan.json": ("modbus_scan", "application/json"),
                    "transactions.jsonl": ("modbus_transactions", "application/x-ndjson"),
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
    def _write_devices_csv(path: Path, devices: object) -> None:
        rows = list(devices)
        columns = [
            "kind", "ip", "port", "state", "confidence", "unit_id",
            "vendor_name", "product_code", "revision", "latency_ms",
            "mac", "fieldbus_type", "module_version", "hostname", "dhcp",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    key: (f"'{value}" if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value)
                    for key, value in row.items()
                })

    @staticmethod
    def _write_plot(
        png_path: Path,
        pdf_path: Path,
        request: ModbusScanRequest,
        summary: dict[str, object],
    ) -> None:
        devices = list(summary["devices"])
        with plt.rc_context({
            "figure.facecolor": "#07100D",
            "axes.facecolor": "#0C1914",
            "axes.edgecolor": "#284136",
            "axes.labelcolor": "#E9EEE9",
            "xtick.color": "#8CA095",
            "ytick.color": "#8CA095",
            "text.color": "#E9EEE9",
            "font.family": "DejaVu Sans",
        }):
            figure, axis = plt.subplots(figsize=(12, 5.2), constrained_layout=True)
            axis.axis("off")
            evidence_class = "SIMULATED EVIDENCE · " if request.mode == "simulator" else ""
            axis.text(
                0.02, 0.95,
                f"{evidence_class}READ-ONLY NETWORK DISCOVERY · {summary['scope']['subnet']}",
                fontsize=16,
                weight="bold",
                va="top",
            )
            axis.text(
                0.02, 0.87,
                f"{len(devices)} device(s) · {summary['scope']['probed_hosts']} hosts probed · 0 writes",
                color="#5CFF9A",
                fontsize=11,
                va="top",
            )
            y = 0.75
            if not devices:
                axis.text(0.02, y, "NO MODBUS OR ANYBUS IDENTITIES OBSERVED", color="#FFBD4A")
            for device in devices[:12]:
                identity = (
                    device.get("product_code")
                    or device.get("fieldbus_type")
                    or "Identity unavailable"
                )
                detail = device.get("vendor_name") or device.get("module_version") or ""
                axis.text(0.04, y, str(device.get("ip", "unknown")), fontsize=13, weight="bold")
                axis.text(0.27, y, str(identity), fontsize=12, color="#FFBD4A")
                axis.text(0.58, y, str(detail), fontsize=11)
                axis.text(0.82, y, str(device.get("state", "")), fontsize=10, color="#8CA095")
                y -= 0.075
            figure.suptitle(
                f"{evidence_class}FIELD JOURNAL · {request.label.strip() or 'MODBUS NETWORK DISCOVERY'}"
            )
            figure.savefig(png_path, dpi=150, metadata={"Software": "Remote Dan Lite"})
            figure.savefig(pdf_path, format="pdf", metadata={
                "Title": f"Remote Dan Lite Modbus discovery: {request.label}",
                "Author": "Field Journal",
                "Subject": "Read-only Modbus and Anybus network discovery evidence",
            })
            plt.close(figure)
