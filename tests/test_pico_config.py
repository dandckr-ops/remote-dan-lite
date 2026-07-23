from __future__ import annotations

import numpy as np
import pytest

from remote_dan.capture import CaptureRequest, ScopeChannelConfig, resolve_capture_channels
from remote_dan.pico import PicoPS2000ABackend


def test_capture_channel_resolution_keeps_network_fixed_and_scope_configurable() -> None:
    network = resolve_capture_channels(CaptureRequest(label="network"))
    assert [(item.channel, item.label, item.enabled) for item in network] == [
        ("A", "VBAT", True),
        ("B", "CAN-H", True),
        ("C", "CAN-L", True),
        ("D", "TRIG", False),
    ]

    injector = resolve_capture_channels(
        CaptureRequest(label="injector", profile="injector-primary")
    )
    assert injector[0].label == "Injector primary"
    assert injector[0].input_range_v == 20.0
    assert injector[0].attenuation == 20.0
    assert injector[0].external_range_v == 400.0

    custom = (
        ScopeChannelConfig("A", True, "custom", 0.5, attenuation=10.0, coupling="AC"),
        ScopeChannelConfig("B", False, "B", 20.0),
        ScopeChannelConfig("C", False, "C", 20.0),
        ScopeChannelConfig("D", False, "D", 20.0),
    )
    assert resolve_capture_channels(
        CaptureRequest(label="custom", profile="general", channels=custom)
    ) == custom


def test_capture_channel_resolution_rejects_invalid_direct_requests() -> None:
    with pytest.raises(ValueError, match="A, B, C, and D"):
        resolve_capture_channels(
            CaptureRequest(
                label="partial",
                profile="general",
                channels=(ScopeChannelConfig("A", True, "A", 20.0),),
            )
        )

    unsafe = tuple(
        ScopeChannelConfig(
            letter,
            letter == "A",
            letter,
            50.0 if letter == "A" else 20.0,
            attenuation=20.0 if letter == "A" else 1.0,
        )
        for letter in ("A", "B", "C", "D")
    )
    with pytest.raises(ValueError, match="unsupported input range"):
        resolve_capture_channels(
            CaptureRequest(label="unsafe", profile="general", channels=unsafe)
        )


def test_pico_range_mapping_and_attenuation_conversion_are_exact() -> None:
    expected = {
        0.02: 1,
        0.05: 2,
        0.1: 3,
        0.2: 4,
        0.5: 5,
        1.0: 6,
        2.0: 7,
        5.0: 8,
        10.0: 9,
        20.0: 10,
    }
    assert {
        value: PicoPS2000ABackend.range_index(value) for value in expected
    } == expected
    with pytest.raises(ValueError, match="unsupported 2406B"):
        PicoPS2000ABackend.range_index(0.01)
    with pytest.raises(ValueError, match="unsupported 2406B"):
        PicoPS2000ABackend.range_index(50.0)

    converted = PicoPS2000ABackend._to_volts(
        np.array([-32767, 0, 32767], dtype=np.int16),
        range_index=10,
        max_adc=32767,
        attenuation=20.0,
    )
    assert converted.tolist() == pytest.approx([-400.0, 0.0, 400.0])
