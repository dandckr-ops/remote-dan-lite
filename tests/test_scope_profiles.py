from __future__ import annotations

import pytest

from remote_dan.capture import PRESETS, SCOPE_PROFILES, resolve_scope_profile


def test_scope_profiles_offer_automotive_starting_points_and_long_windows() -> None:
    assert list(PRESETS) == ["short", "medium", "long", "1s", "2s", "5s", "10s"]
    assert PRESETS["1s"].duration_ms == pytest.approx(999.995)
    assert PRESETS["2s"].duration_ms == pytest.approx(1999.99)
    assert PRESETS["5s"].duration_ms == pytest.approx(4999.98)
    assert PRESETS["10s"].duration_ms == pytest.approx(9999.96)
    assert all(preset.samples % 5_000 == 0 for preset in PRESETS.values())

    assert set(SCOPE_PROFILES) == {
        "general",
        "secondary-ignition",
        "crankshaft-vr",
        "crankshaft-hall",
        "injector-primary",
    }

    secondary = resolve_scope_profile("secondary-ignition")
    assert secondary.preset == "medium"
    assert secondary.channels[0].channel == "A"
    assert secondary.channels[0].label == "Secondary pickup"
    assert secondary.channels[0].coupling == "AC"
    assert secondary.channels[0].input_range_v == 20.0
    assert secondary.channels[0].attenuation == 1.0
    assert "never connect" in secondary.warning.lower()
    assert "pickup" in secondary.warning.lower()

    crank_vr = resolve_scope_profile("crankshaft-vr")
    assert crank_vr.preset == "2s"
    assert crank_vr.channels[0].coupling == "AC"
    assert crank_vr.channels[0].input_range_v == 20.0

    crank_hall = resolve_scope_profile("crankshaft-hall")
    assert crank_hall.preset == "2s"
    assert crank_hall.channels[0].coupling == "DC"
    assert crank_hall.channels[0].input_range_v == 10.0

    injector = resolve_scope_profile("injector-primary")
    assert injector.preset == "long"
    assert injector.channels[0].attenuation == 20.0
    assert injector.channels[0].external_range_v == 400.0


def test_unknown_scope_profile_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown scope profile"):
        resolve_scope_profile("magic")
