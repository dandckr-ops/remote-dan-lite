from __future__ import annotations

import ctypes
import time

import numpy as np

from remote_dan.capture import (
    CaptureData,
    CaptureRequest,
    ScopeChannelConfig,
    resolve_capture_channels,
    resolve_preset,
)


def streaming_interval(sample_interval_us: float) -> tuple[int, str, float]:
    if sample_interval_us <= 0:
        raise ValueError("sample interval must be positive")
    if sample_interval_us < 1.0:
        nanoseconds = round(sample_interval_us * 1000.0)
        if nanoseconds < 1:
            raise ValueError("sample interval is below the PS2000A nanosecond limit")
        return nanoseconds, "PS2000A_NS", 0.001
    microseconds = round(sample_interval_us)
    if abs(microseconds - sample_interval_us) > 1e-9:
        raise ValueError("sample intervals above 1 us must use whole microseconds")
    return microseconds, "PS2000A_US", 1.0


class PicoPS2000ABackend:
    """Profile-driven PS2000A streaming acquisition for the 2406B field harness."""

    name = "ps2000a"
    _RANGE_MV = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000)
    _RANGE_INDEX_BY_V = {millivolts / 1000.0: index for index, millivolts in enumerate(_RANGE_MV)}

    def __init__(self, *, buffer_samples: int = 5_000) -> None:
        self.buffer_samples = buffer_samples

    @classmethod
    def range_index(cls, input_range_v: float) -> int:
        from remote_dan.capture import INPUT_RANGES_V

        if input_range_v not in INPUT_RANGES_V:
            raise ValueError(f"unsupported 2406B input range: {input_range_v} V")
        try:
            return cls._RANGE_INDEX_BY_V[input_range_v]
        except KeyError as exc:
            raise ValueError(f"unsupported 2406B input range: {input_range_v} V") from exc

    @staticmethod
    def _to_volts(raw: np.ndarray, range_index: int, max_adc: int, attenuation: float) -> np.ndarray:
        millivolts = raw.astype(np.float64) * PicoPS2000ABackend._RANGE_MV[range_index] / max_adc
        return millivolts * attenuation / 1000.0

    def capture(self, request: CaptureRequest) -> CaptureData:
        from picosdk.functions import assert_pico_ok
        from picosdk.ps2000a import ps2000a as ps

        preset = resolve_preset(request.preset)
        configs = resolve_capture_channels(request)
        enabled_configs = tuple(config for config in configs if config.enabled)
        total_samples = preset.samples
        chunk_samples = min(self.buffer_samples, total_samples)
        if total_samples % chunk_samples:
            raise ValueError("capture sample count must be divisible by the streaming buffer")

        channel_ids = {
            letter: ps.PS2000A_CHANNEL[f"PS2000A_CHANNEL_{letter}"]
            for letter in ("A", "B", "C", "D")
        }
        coupling_ids = {
            "AC": ps.PS2000A_COUPLING["PS2000A_AC"],
            "DC": ps.PS2000A_COUPLING["PS2000A_DC"],
        }
        ratio_none = ps.PS2000A_RATIO_MODE["PS2000A_RATIO_MODE_NONE"]

        handle = ctypes.c_int16()
        opened = False
        try:
            assert_pico_ok(ps.ps2000aOpenUnit(ctypes.byref(handle), None))
            opened = True
            range_indexes: dict[str, int] = {}
            for config in configs:
                range_index = self.range_index(config.input_range_v)
                range_indexes[config.channel] = range_index
                assert_pico_ok(
                    ps.ps2000aSetChannel(
                        handle,
                        channel_ids[config.channel],
                        int(config.enabled),
                        coupling_ids[config.coupling],
                        range_index,
                        0.0,
                    )
                )

            buffers = {
                config.channel: np.zeros(chunk_samples, dtype=np.int16)
                for config in enabled_configs
            }
            complete = {
                config.channel: np.zeros(total_samples, dtype=np.int16)
                for config in enabled_configs
            }
            for config in enabled_configs:
                assert_pico_ok(
                    ps.ps2000aSetDataBuffers(
                        handle,
                        channel_ids[config.channel],
                        buffers[config.channel].ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                        None,
                        chunk_samples,
                        0,
                        ratio_none,
                    )
                )

            interval_value, interval_unit, interval_to_us = streaming_interval(
                preset.sample_interval_us
            )
            sample_interval = ctypes.c_int32(interval_value)
            assert_pico_ok(
                ps.ps2000aRunStreaming(
                    handle,
                    ctypes.byref(sample_interval),
                    ps.PS2000A_TIME_UNITS[interval_unit],
                    0,
                    total_samples,
                    1,
                    1,
                    ratio_none,
                    chunk_samples,
                )
            )

            state = {"next": 0, "called": False, "auto_stop": False, "overflow_mask": 0}

            def streaming_callback(
                _handle: int,
                number_of_samples: int,
                start_index: int,
                overflow: int,
                _trigger_at: int,
                _triggered: int,
                auto_stop: int,
                _parameter: object,
            ) -> None:
                state["called"] = True
                state["overflow_mask"] = int(state["overflow_mask"]) | int(overflow)
                remaining = total_samples - int(state["next"])
                count = min(int(number_of_samples), remaining)
                destination_start = int(state["next"])
                destination_end = destination_start + count
                source_end = int(start_index) + count
                for config in enabled_configs:
                    key = config.channel
                    complete[key][destination_start:destination_end] = buffers[key][
                        int(start_index):source_end
                    ]
                state["next"] = destination_end
                state["auto_stop"] = bool(auto_stop)

            callback = ps.StreamingReadyType(streaming_callback)
            deadline = time.monotonic() + max(90.0, preset.duration_ms / 1000.0 * 3.0 + 10.0)
            while int(state["next"]) < total_samples and not bool(state["auto_stop"]):
                state["called"] = False
                assert_pico_ok(ps.ps2000aGetStreamingLatestValues(handle, callback, None))
                if not state["called"]:
                    time.sleep(0.002)
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"PS2000A stream timed out at {state['next']}/{total_samples} samples"
                    )

            if int(state["next"]) != total_samples:
                raise RuntimeError(
                    f"PS2000A capture ended at {state['next']}/{total_samples} samples"
                )

            max_adc = ctypes.c_int16()
            assert_pico_ok(ps.ps2000aMaximumValue(handle, ctypes.byref(max_adc)))
            channels = {
                config.label: self._to_volts(
                    complete[config.channel],
                    range_indexes[config.channel],
                    max_adc.value,
                    config.attenuation,
                )
                for config in enabled_configs
            }
            overflow_mask = int(state["overflow_mask"])
            overflow_channels = tuple(
                config.channel
                for config in enabled_configs
                if overflow_mask & (1 << channel_ids[config.channel])
            )
            actual_interval_us = float(sample_interval.value) * interval_to_us
            time_us = np.arange(total_samples, dtype=np.float64) * actual_interval_us
            actual_preset = type(preset)(
                name=preset.name,
                samples=preset.samples,
                sample_interval_us=actual_interval_us,
            )
            return CaptureData(
                backend=self.name,
                preset=actual_preset,
                time_us=time_us,
                channels=channels,
                profile=request.profile,
                channel_configs=configs,
                overflow_channels=overflow_channels,
            )
        finally:
            if opened:
                try:
                    ps.ps2000aStop(handle)
                finally:
                    ps.ps2000aCloseUnit(handle)
