from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from remote_dan.capture import (
    CaptureData,
    CaptureManager,
    CaptureRequest,
    SimulatorBackend,
    resolve_preset,
)


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
    assert saved["summary"]["preview_channels"] == ["CAN-H", "CAN-L"]
    assert "VBAT" not in saved["summary"]["preview_channels"]


def test_capture_labels_are_sanitized_for_run_identifiers(tmp_path: Path) -> None:
    manager = CaptureManager(tmp_path, backend=SimulatorBackend(seed=1))

    manifest = manager.run(CaptureRequest(label="  Pump #2 / start?!  ", preset="short", mode="simulator"))

    assert manifest["run_id"].endswith("-pump-2-start-short")
    assert "/" not in manifest["run_id"]


def test_network_overview_plot_keeps_vbat_digital_and_plots_only_can(tmp_path: Path) -> None:
    preset = resolve_preset("short")
    samples = preset.samples
    data = CaptureData(
        backend="simulator",
        preset=preset,
        time_us=np.arange(samples, dtype=np.float64) * preset.sample_interval_us,
        channels={
            "VBAT": np.full(samples, 12.25),
            "CAN-H": np.full(samples, 3.5),
            "CAN-L": np.full(samples, 1.5),
        },
    )
    figure = MagicMock()
    axis = MagicMock()

    with patch("remote_dan.capture.plt.subplots", return_value=(figure, axis)) as subplots:
        with patch("remote_dan.capture.plt.close"):
            CaptureManager._write_plot(
                tmp_path / "overview.png",
                tmp_path / "report.pdf",
                CaptureRequest(label="network proof", preset="short"),
                data,
            )

    assert subplots.call_args.args[:2] == (1, 1)
    plotted_labels = [call.kwargs.get("label") for call in axis.plot.call_args_list]
    assert plotted_labels == ["CAN-H", "CAN-L"]
    axis.set_ylabel.assert_called_once_with("BUS / V")
    figure.savefig.assert_any_call(
        tmp_path / "overview.png",
        dpi=150,
        metadata={"Software": "Remote Dan Lite"},
    )


def test_can_analysis_preset_persists_structured_signal_intelligence(tmp_path: Path) -> None:
    preset = resolve_preset("can-analysis")
    assert preset.samples == 250_000
    assert preset.sample_interval_us == 0.1
    assert preset.duration_ms == 24.9999

    manager = CaptureManager(tmp_path, backend=SimulatorBackend(seed=7))
    manifest = manager.run(
        CaptureRequest(
            label="analysis evidence",
            preset="can-analysis",
            mode="simulator",
            capture_type="can",
            profile="network",
        )
    )

    analysis = manifest["summary"]["can_analysis"]
    assert analysis["status"] == "analyzed"
    assert analysis["nominal_bitrate_bps"] == 500_000
    assert analysis["bus_type"] == "Classical CAN"
    assert analysis["frame_count"] > 0
    assert 0.0 < analysis["bus_load_percent"] < 100.0
    assert analysis["protocol"]["name"] == "SAE J1939"

    saved_summary = json.loads(
        (tmp_path / manifest["run_id"] / "summary.json").read_text()
    )
    assert saved_summary["can_analysis"] == analysis


def test_can_workspace_decodes_reversed_pair_and_preserves_polarity_warning(
    tmp_path: Path,
) -> None:
    class ReversedCanBackend:
        def capture(self, request: CaptureRequest) -> CaptureData:
            data = SimulatorBackend(seed=2406).capture(request)
            channels = dict(data.channels)
            channels["CAN-H"], channels["CAN-L"] = (
                data.channels["CAN-L"],
                data.channels["CAN-H"],
            )
            return CaptureData(
                backend=data.backend,
                preset=data.preset,
                time_us=data.time_us,
                channels=channels,
                profile=data.profile,
                channel_configs=data.channel_configs,
                overflow_channels=data.overflow_channels,
            )

    manager = CaptureManager(tmp_path, backend=ReversedCanBackend())
    manifest = manager.run(CaptureRequest(
        label="reversed CAN evidence",
        preset="can-analysis",
        mode="simulator",
        capture_type="can",
        profile="network",
    ))

    summary = manifest["summary"]
    assert summary["can_polarity"] == "reversed"
    assert summary["can_analysis"]["crc_valid_header_count"] > 0
    assert any(
        "reversed" in warning.lower()
        for warning in summary["can_analysis"]["warnings"]
    )
