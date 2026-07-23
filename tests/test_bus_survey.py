from __future__ import annotations

import json
from pathlib import Path

import pytest

from remote_dan.bus_survey import (
    BusSurveyManager,
    BusSurveyRequest,
    BusSurveySimulatorBackend,
)
from remote_dan.database import EvidenceDatabase


def test_bus_survey_packages_three_segments_classification_and_lineage(
    tmp_path: Path,
) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    manager = BusSurveyManager(
        tmp_path / "captures",
        backend=BusSurveySimulatorBackend(),
        database=database,
    )

    manifest = manager.run(BusSurveyRequest(
        label="unknown CAN survey",
        harness="can-network",
        mode="simulator",
    ))
    run_dir = tmp_path / "captures" / manifest["run_id"]

    assert manifest["capture_type"] == "bus_survey"
    assert manifest["profile"] == "bus-sniffer"
    assert manifest["backend"] == "bus-survey-simulator"
    assert manifest["summary"]["classification"]["family"] == "CAN-family"
    assert manifest["summary"]["classification"]["workspace"] == "can"
    assert [item["name"] for item in manifest["summary"]["segments"]] == [
        "fast", "context", "sparse"
    ]
    assert set(manifest["artifacts"]) == {
        "fast.csv",
        "context.csv",
        "sparse.csv",
        "segments.json",
        "overview.png",
        "report.pdf",
        "summary.json",
        "manifest.json",
    }
    for name in manifest["artifacts"]:
        assert (run_dir / name).is_file()
        assert (run_dir / name).stat().st_size > 0

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["writes_performed"] == 0
    assert summary["physical_connection_required"] == "can-network"
    assert summary["acquisition_provenance"]["mode"] == "simulator"
    assert len(summary["acquisition_provenance"]["segments"]) == 3
    assert summary["acquisition_provenance"]["segments"][0]["channel_configs"]
    assert summary["classification"]["confidence"] == "medium"
    assert summary["safety_attestations"] == {
        "common_reference": False,
        "low_voltage_domain": False,
        "passive_inputs_only": False,
        "probe_rating_and_attenuation": False,
    }

    record = database.get_capture(manifest["capture_id"])
    assert record is not None
    assert record["status"] == "complete"
    assert record["capture_type"] == "bus_survey"
    assert len(record["artifacts"]) == 8


def test_bus_survey_simulator_uses_selected_protected_differential_path(
    tmp_path: Path,
) -> None:
    manager = BusSurveyManager(
        tmp_path,
        backend=BusSurveySimulatorBackend(),
    )

    manifest = manager.run(BusSurveyRequest(
        label="unknown differential survey",
        harness="protected-differential",
        mode="simulator",
    ))

    classification = manifest["summary"]["classification"]
    assert classification["family"] == "RS-485/422-like balanced UART"
    assert classification["workspace"] == "bus-sniffer"
    assert classification["candidate_bitrate_bps"] == 9600


def test_bus_survey_rejects_unverified_harness_before_capture(tmp_path: Path) -> None:
    manager = BusSurveyManager(tmp_path, backend=BusSurveySimulatorBackend())

    try:
        manager.run(BusSurveyRequest(
            label="unsafe",
            harness="unverified",
            mode="simulator",
        ))
    except ValueError as exc:
        assert "verified harness" in str(exc)
    else:
        raise AssertionError("unverified harness should fail")

    assert list(tmp_path.glob("*/manifest.json")) == []


def test_hardware_bus_survey_requires_and_persists_all_safety_attestations(
    tmp_path: Path,
) -> None:
    manager = BusSurveyManager(tmp_path, backend=BusSurveySimulatorBackend())

    with pytest.raises(ValueError, match="safety attestations"):
        manager.run(BusSurveyRequest(
            label="blocked hardware survey",
            harness="can-network",
            mode="hardware",
        ))
    assert list(tmp_path.glob("*/manifest.json")) == []

    manifest = manager.run(BusSurveyRequest(
        label="attested hardware contract",
        harness="can-network",
        mode="hardware",
        low_voltage_confirmed=True,
        common_reference_confirmed=True,
        probe_rating_confirmed=True,
        passive_only_confirmed=True,
    ))
    assert all(manifest["summary"]["safety_attestations"].values())
