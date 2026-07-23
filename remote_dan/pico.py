from __future__ import annotations

import ctypes
import time

import numpy as np

from remote_dan.capture import CaptureData, CaptureRequest, resolve_preset


class PicoPS2000ABackend:
    """Three-channel PS2000A streaming acquisition for the 2406B field harness."""

    name = "ps2000a"
    _RANGE_MV = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000]

    def __init__(
        self,
        *,
        vbat_attenuation: float = 20.0,
        can_h_attenuation: float = 1.0,
        can_l_attenuation: float = 1.0,
        buffer_samples: int = 5_000,
    ) -> None:
        self.vbat_attenuation = vbat_attenuation
        self.can_h_attenuation = can_h_attenuation
        self.can_l_attenuation = can_l_attenuation
        self.buffer_samples = buffer_samples

    @staticmethod
    def _to_volts(raw: np.ndarray, range_index: int, max_adc: int, attenuation: float) -> np.ndarray:
        millivolts = raw.astype(np.float64) * PicoPS2000ABackend._RANGE_MV[range_index] / max_adc
        return millivolts * attenuation / 1000.0

    def capture(self, request: CaptureRequest) -> CaptureData:
        from picosdk.functions import assert_pico_ok
        from picosdk.ps2000a import ps2000a as ps

        preset = resolve_preset(request.preset)
        total_samples = preset.samples
        chunk_samples = min(self.buffer_samples, total_samples)
        if total_samples % chunk_samples:
            raise ValueError("capture sample count must be divisible by the streaming buffer")

        channel_a = ps.PS2000A_CHANNEL["PS2000A_CHANNEL_A"]
        channel_b = ps.PS2000A_CHANNEL["PS2000A_CHANNEL_B"]
        channel_c = ps.PS2000A_CHANNEL["PS2000A_CHANNEL_C"]
        channel_d = ps.PS2000A_CHANNEL["PS2000A_CHANNEL_D"]
        dc = ps.PS2000A_COUPLING["PS2000A_DC"]
        ratio_none = ps.PS2000A_RATIO_MODE["PS2000A_RATIO_MODE_NONE"]
        a_range = ps.PS2000A_RANGE["PS2000A_1V"]
        bc_range = ps.PS2000A_RANGE["PS2000A_10V"]

        handle = ctypes.c_int16()
        opened = False
        try:
            assert_pico_ok(ps.ps2000aOpenUnit(ctypes.byref(handle), None))
            opened = True
            for channel, enabled, voltage_range in (
                (channel_a, 1, a_range),
                (channel_b, 1, bc_range),
                (channel_c, 1, bc_range),
                (channel_d, 0, bc_range),
            ):
                assert_pico_ok(
                    ps.ps2000aSetChannel(handle, channel, enabled, dc, voltage_range, 0.0)
                )

            buffers = {
                "A": np.zeros(chunk_samples, dtype=np.int16),
                "B": np.zeros(chunk_samples, dtype=np.int16),
                "C": np.zeros(chunk_samples, dtype=np.int16),
            }
            complete = {
                "A": np.zeros(total_samples, dtype=np.int16),
                "B": np.zeros(total_samples, dtype=np.int16),
                "C": np.zeros(total_samples, dtype=np.int16),
            }
            for key, channel in (("A", channel_a), ("B", channel_b), ("C", channel_c)):
                assert_pico_ok(
                    ps.ps2000aSetDataBuffers(
                        handle,
                        channel,
                        buffers[key].ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                        None,
                        chunk_samples,
                        0,
                        ratio_none,
                    )
                )

            sample_interval = ctypes.c_int32(preset.sample_interval_us)
            assert_pico_ok(
                ps.ps2000aRunStreaming(
                    handle,
                    ctypes.byref(sample_interval),
                    ps.PS2000A_TIME_UNITS["PS2000A_US"],
                    0,
                    total_samples,
                    1,
                    1,
                    ratio_none,
                    chunk_samples,
                )
            )

            state = {"next": 0, "called": False, "auto_stop": False}

            def streaming_callback(
                _handle: int,
                number_of_samples: int,
                start_index: int,
                _overflow: int,
                _trigger_at: int,
                _triggered: int,
                auto_stop: int,
                _parameter: object,
            ) -> None:
                state["called"] = True
                remaining = total_samples - int(state["next"])
                count = min(int(number_of_samples), remaining)
                destination_start = int(state["next"])
                destination_end = destination_start + count
                source_end = int(start_index) + count
                for key in ("A", "B", "C"):
                    complete[key][destination_start:destination_end] = buffers[key][int(start_index):source_end]
                state["next"] = destination_end
                state["auto_stop"] = bool(auto_stop)

            callback = ps.StreamingReadyType(streaming_callback)
            deadline = time.monotonic() + 90.0
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
                "VBAT": self._to_volts(complete["A"], a_range, max_adc.value, self.vbat_attenuation),
                "CAN-H": self._to_volts(complete["B"], bc_range, max_adc.value, self.can_h_attenuation),
                "CAN-L": self._to_volts(complete["C"], bc_range, max_adc.value, self.can_l_attenuation),
            }
            time_us = np.arange(total_samples, dtype=np.float64) * int(sample_interval.value)
            actual_preset = type(preset)(
                name=preset.name,
                samples=preset.samples,
                sample_interval_us=int(sample_interval.value),
            )
            return CaptureData(
                backend=self.name,
                preset=actual_preset,
                time_us=time_us,
                channels=channels,
            )
        finally:
            if opened:
                try:
                    ps.ps2000aStop(handle)
                finally:
                    ps.ps2000aCloseUnit(handle)
