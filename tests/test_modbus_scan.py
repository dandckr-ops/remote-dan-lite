from __future__ import annotations

import csv
import json
from pathlib import Path

from remote_dan.database import EvidenceDatabase
from remote_dan.modbus_scan import (
    ModbusScanManager,
    ModbusScanRequest,
    ModbusSimulatorBackend,
)


def test_modbus_simulator_persists_scan_inventory_report_and_lineage(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    manager = ModbusScanManager(
        tmp_path / "captures",
        backend=ModbusSimulatorBackend(),
        database=database,
    )

    manifest = manager.run(
        ModbusScanRequest(
            label="plant network discovery",
            subnet="192.168.50.0/24",
            interface="eth0",
            connected_networks=(
                {
                    "interface": "eth0",
                    "address": "192.168.50.10",
                    "network": "192.168.50.0/24",
                },
            ),
            connect_timeout_s=0.3,
            workers=4,
            mode="simulator",
        )
    )
    run_dir = tmp_path / "captures" / manifest["run_id"]

    assert manifest["capture_type"] == "modbus_scan"
    assert manifest["profile"] == "modbus"
    assert manifest["backend"] == "modbus-simulator"
    assert manifest["capture_id"] > 0
    assert manifest["summary"]["device_count"] == 2
    assert manifest["summary"]["writes_performed"] == 0
    assert {item["kind"] for item in manifest["summary"]["devices"]} == {
        "anybus_hicp",
        "modbus_tcp",
    }
    assert set(manifest["artifacts"]) == {
        "devices.csv",
        "scan.json",
        "transactions.jsonl",
        "overview.png",
        "report.pdf",
        "summary.json",
        "manifest.json",
    }
    for name in manifest["artifacts"]:
        assert (run_dir / name).is_file()
        assert (run_dir / name).stat().st_size > 0

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["scan_type"] == "read_only_modbus_discovery"
    assert summary["scope"]["subnet"] == "192.168.50.0/24"
    assert summary["scope"]["interface"] == "eth0"
    assert summary["outcome_counts"] == {"simulator_no_packets": 1}
    assert summary["requests_used"] == [
        "HMS HICP MODULE SCAN",
        "Modbus 43/14 Read Device Identification",
    ]

    record = database.get_capture(manifest["capture_id"])
    assert record is not None
    assert record["status"] == "complete"
    assert record["capture_type"] == "modbus_scan"
    assert {item["filename"] for item in record["artifacts"]} == set(
        manifest["artifacts"]
    )


def test_modbus_scan_rejects_unsafe_or_unconnected_subnet_before_artifacts(
    tmp_path: Path,
) -> None:
    manager = ModbusScanManager(tmp_path, backend=ModbusSimulatorBackend())

    try:
        manager.run(
            ModbusScanRequest(
                label="unsafe",
                subnet="10.20.0.0/16",
                interface="eth0",
                connected_networks=(
                    {
                        "interface": "eth0",
                        "address": "192.168.50.10",
                        "network": "192.168.50.0/24",
                    },
                ),
                mode="simulator",
            )
        )
    except ValueError as exc:
        assert "at most" in str(exc) or "connected" in str(exc)
    else:
        raise AssertionError("unsafe scan scope should fail")

    assert list(tmp_path.glob("*/manifest.json")) == []


def test_device_csv_escapes_spreadsheet_formula_prefixes(tmp_path: Path) -> None:
    path = tmp_path / "devices.csv"
    ModbusScanManager._write_devices_csv(path, [{
        "kind": "modbus_tcp",
        "ip": "192.168.1.20",
        "vendor_name": "=HYPERLINK(\"https://invalid\")",
        "product_code": "+cmd",
    }])

    with path.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["vendor_name"].startswith("'=HYPERLINK")
    assert row["product_code"].startswith("'+")
