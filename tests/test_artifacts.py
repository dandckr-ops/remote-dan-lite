from __future__ import annotations

import json
from pathlib import Path

from remote_dan.capture import CaptureManager, CaptureRequest, SimulatorBackend


def test_capture_manager_writes_field_evidence_artifacts(tmp_path: Path) -> None:
    manager = CaptureManager(tmp_path, backend=SimulatorBackend(seed=99))

    manifest = manager.run(CaptureRequest(label="CAN wake-up", preset="short", mode="simulator"))
    run_dir = tmp_path / manifest["run_id"]

    assert manifest["backend"] == "simulator"
    assert manifest["label"] == "CAN wake-up"
    assert set(manifest["artifacts"]) == {
        "capture.csv",
        "manifest.json",
        "overview.png",
        "report.pdf",
        "summary.json",
    }
    for artifact in manifest["artifacts"]:
        assert (run_dir / artifact).stat().st_size > 0

    saved = json.loads((run_dir / "manifest.json").read_text())
    assert saved == manifest
    assert saved["sha256"]["capture.csv"]


def test_capture_labels_are_sanitized_for_run_identifiers(tmp_path: Path) -> None:
    manager = CaptureManager(tmp_path, backend=SimulatorBackend(seed=1))

    manifest = manager.run(CaptureRequest(label="  Pump #2 / start?!  ", preset="short", mode="simulator"))

    assert manifest["run_id"].endswith("-pump-2-start-short")
    assert "/" not in manifest["run_id"]
