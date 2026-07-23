from __future__ import annotations

import numpy as np

from remote_dan.bus_sniffer import SurveySegment, analyze_bus_survey
from remote_dan.capture import CaptureRequest, SimulatorBackend


def _uart_logic(
    payload: bytes,
    *,
    baud: int = 9600,
    sample_interval_us: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    samples_per_bit = round((1_000_000 / baud) / sample_interval_us)
    bits: list[int] = [1] * 12
    for value in payload:
        bits.extend([0])
        bits.extend((value >> index) & 1 for index in range(8))
        bits.extend([1, 1])
    bits.extend([1] * 12)
    logic = np.repeat(np.asarray(bits, dtype=np.float64), samples_per_bit)
    time_us = np.arange(logic.size, dtype=np.float64) * sample_interval_us
    return time_us, logic


def _segment(
    time_us: np.ndarray,
    channels: dict[str, np.ndarray],
    *,
    name: str = "fast",
    overflow: tuple[str, ...] = (),
) -> SurveySegment:
    return SurveySegment(
        name=name,
        time_us=time_us,
        channels=channels,
        overflow_channels=overflow,
    )


def test_crc_valid_can_routes_to_can_workspace_and_commissioned_harness() -> None:
    data = SimulatorBackend(seed=2406).capture(CaptureRequest(
        label="CAN survey",
        preset="can-analysis",
        mode="simulator",
        capture_type="bus_survey",
        profile="network",
    ))

    result = analyze_bus_survey([
        _segment(data.time_us, data.channels),
    ], harness="can-network")

    assert result["status"] == "classified"
    assert result["family"] == "CAN-family"
    assert result["confidence"] == "medium"
    assert result["candidate_bitrate_bps"] == 500_000
    assert result["workspace"] == "can"
    assert "commissioned CAN harness" in result["input_device"]
    assert "CRC-valid" in " ".join(result["evidence"])


def test_differential_uart_shape_routes_to_serial_with_isolated_rs485_receiver() -> None:
    time_us, logic = _uart_logic(b"\x01\x03\x00\x00\x00\x02\xC4\x0B" * 4)
    positive = 2.5 + np.where(logic > 0.5, 1.5, -1.5)
    negative = 2.5 - np.where(logic > 0.5, 1.5, -1.5)

    result = analyze_bus_survey([
        _segment(time_us, {"B": positive, "C": negative}),
        _segment(time_us, {"B": positive, "C": negative}, name="context"),
    ], harness="protected-differential")

    assert result["status"] == "classified"
    assert result["family"] == "RS-485/422-like balanced UART"
    assert result["confidence"] == "medium"
    assert result["candidate_bitrate_bps"] == 9600
    assert result["workspace"] == "bus-sniffer"
    assert "isolated RS-485" in result["input_device"]
    assert "application protocol unresolved" in result["boundary"].lower()


def test_bipolar_rs232_routes_to_serial_but_does_not_assume_c662_compatibility() -> None:
    time_us, logic = _uart_logic(b"SEL-2032 PORT 1=>" * 3)
    signal = np.where(logic > 0.5, -8.0, 8.0)

    result = analyze_bus_survey([
        _segment(time_us, {"A": signal}),
        _segment(time_us, {"A": signal}, name="context"),
    ], harness="protected-single-ended")

    assert result["family"] == "RS-232 asynchronous candidate"
    assert result["candidate_bitrate_bps"] == 9600
    assert result["workspace"] == "bus-sniffer"
    assert "C662 only after" in result["input_device"]


def test_ttl_uart_routes_to_serial_with_protected_ttl_receiver() -> None:
    time_us, logic = _uart_logic(b"hello world\r\n" * 4)
    signal = logic * 5.0

    result = analyze_bus_survey([
        _segment(time_us, {"A": signal}),
        _segment(time_us, {"A": signal}, name="context"),
    ], harness="protected-single-ended")

    assert result["family"] == "TTL UART asynchronous candidate"
    assert result["candidate_bitrate_bps"] == 9600
    assert result["workspace"] == "bus-sniffer"
    assert "TTL" in result["input_device"]


def test_twelve_volt_single_wire_remains_family_candidate_not_false_lin_claim() -> None:
    time_us, logic = _uart_logic(b"\x55\x10\x81\x7E" * 4, baud=19_200)
    signal = logic * 12.0

    result = analyze_bus_survey([
        _segment(time_us, {"A": signal}),
    ], harness="protected-single-ended")

    assert result["family"] == "12 V single-wire digital candidate"
    assert result["confidence"] == "low"
    assert result["workspace"] == "scope"
    assert "LIN / K-Line / PWM unresolved" in result["boundary"]


def test_periodic_clock_does_not_false_positive_as_ttl_uart() -> None:
    time_us = np.arange(60_000, dtype=np.float64) * 2.0
    clock = ((time_us // 52.0) % 2.0) * 5.0

    result = analyze_bus_survey([
        _segment(time_us, {"A": clock}),
        _segment(time_us, {"A": clock}, name="context"),
    ], harness="protected-single-ended")

    assert result["family"] == "General analog or unresolved signal"
    assert result["workspace"] == "scope"
    assert result["confidence"] == "low"


def test_periodic_differential_clock_does_not_false_positive_as_rs485() -> None:
    time_us = np.arange(60_000, dtype=np.float64) * 2.0
    state = np.where((time_us // 52.0) % 2.0, 1.5, -1.5)
    channels = {"B": 2.5 + state, "C": 2.5 - state}

    result = analyze_bus_survey([
        _segment(time_us, channels),
        _segment(time_us, channels, name="context"),
    ], harness="protected-differential")

    assert result["status"] == "ambiguous"
    assert result["workspace"] == "scope"


def test_unsafe_common_mode_in_later_window_fails_entire_survey_closed() -> None:
    time_us, logic = _uart_logic(b"\x55\xAA\x33\xCC" * 150, sample_interval_us=2.0)
    safe_state = np.where(logic > 0.5, 1.5, -1.5)
    unsafe_state = np.where(logic > 0.5, 0.8, -0.8)

    result = analyze_bus_survey([
        _segment(time_us, {"B": 2.5 + safe_state, "C": 2.5 - safe_state}),
        _segment(
            time_us,
            {"B": 18.0 + unsafe_state, "C": 18.0 - unsafe_state},
            name="context",
        ),
    ], harness="protected-differential")

    assert result["status"] == "unsafe"
    assert result["confidence"] == "none"
    assert result["workspace"] == "bus-sniffer"
    assert result["features"]["unsafe_common_windows"][0]["segment"] == "context"


def test_later_uart_windows_are_reconciled_instead_of_ignored() -> None:
    fast_time = np.arange(20_000, dtype=np.float64) * 0.5
    fast_noise = 0.2 * np.sin(2 * np.pi * fast_time / 100.0)
    uart_time, logic = _uart_logic(b"multi-window evidence\r\n" * 3)

    result = analyze_bus_survey([
        _segment(fast_time, {"A": fast_noise}),
        _segment(uart_time, {"A": logic * 5.0}, name="context"),
        _segment(uart_time, {"A": logic * 5.0}, name="sparse"),
    ], harness="protected-single-ended")

    assert result["family"] == "TTL UART asynchronous candidate"
    assert result["features"]["uart_validation"]["valid_windows"] >= 2


def test_analog_waveform_stays_in_scope_workspace() -> None:
    time_us = np.arange(20_000, dtype=np.float64) * 2.0
    signal = 2.0 * np.sin(2 * np.pi * time_us / 1000.0)

    result = analyze_bus_survey([
        _segment(time_us, {"A": signal}),
    ], harness="protected-single-ended")

    assert result["family"] == "General analog or unresolved signal"
    assert result["workspace"] == "scope"
    assert result["candidate_bitrate_bps"] is None


def test_silent_bus_fails_closed_without_tab_or_device_claim() -> None:
    time_us = np.arange(20_000, dtype=np.float64) * 2.0

    result = analyze_bus_survey([
        _segment(time_us, {"B": np.full(time_us.size, 2.5), "C": np.full(time_us.size, 2.5)}),
    ], harness="protected-differential")

    assert result["status"] == "no_activity"
    assert result["family"] == "Unresolved — no activity"
    assert result["workspace"] == "bus-sniffer"
    assert result["input_device"] == "No recommendation"


def test_overflow_fails_closed_before_classification() -> None:
    time_us, logic = _uart_logic(b"overflow")

    result = analyze_bus_survey([
        _segment(time_us, {"A": logic * 24.0}, overflow=("A",)),
    ], harness="protected-single-ended")

    assert result["status"] == "invalid_capture"
    assert result["workspace"] == "bus-sniffer"
    assert "over-range" in result["reason"].lower()


def test_unverified_harness_is_rejected_before_analysis() -> None:
    time_us = np.arange(100, dtype=np.float64)
    with np.testing.assert_raises_regex(ValueError, "verified harness"):
        analyze_bus_survey([
            _segment(time_us, {"A": np.zeros(100)}),
        ], harness="unverified")
