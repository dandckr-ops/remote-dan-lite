from __future__ import annotations

import numpy as np
import pytest

from remote_dan.capture import CaptureRequest, SimulatorBackend, resolve_preset


def test_short_preset_has_bounded_capture_shape() -> None:
    preset = resolve_preset("short")

    assert preset.samples == 20_000
    assert preset.sample_interval_us == 2
    assert preset.duration_ms == pytest.approx(39.998)


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown preset"):
        resolve_preset("reckless")


def test_simulator_produces_repeatable_complementary_can_and_battery_channels() -> None:
    backend = SimulatorBackend(seed=2406)
    request = CaptureRequest(label="bench proof", preset="short", mode="simulator")

    first = backend.capture(request)
    second = SimulatorBackend(seed=2406).capture(request)

    assert first.backend == "simulator"
    assert first.channel_names == ("VBAT", "CAN-H", "CAN-L")
    assert first.time_us.shape == (20_000,)
    assert np.array_equal(first.channels["CAN-H"], second.channels["CAN-H"])
    assert 12.0 < float(np.mean(first.channels["VBAT"])) < 15.0
    assert float(np.corrcoef(first.channels["CAN-H"], first.channels["CAN-L"])[0, 1]) < -0.95
