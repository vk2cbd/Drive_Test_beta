import math

import numpy as np

from radio_survey.sdr import SoapySdrplayLevelMeter
from radio_survey.sdr import _measurement_bandwidth_param_hz


def _meter_for_bandwidth(measurement_bandwidth_hz: float) -> SoapySdrplayLevelMeter:
    meter = SoapySdrplayLevelMeter()
    meter._np = np
    meter._sample_rate_hz = 200_000.0
    meter._bandwidth_hz = 200_000.0
    meter._measurement_bandwidth_hz = measurement_bandwidth_hz
    meter._dbm_offset = 0.0
    meter._last_level_dbm = None
    return meter


def test_channel_power_increases_with_measurement_bandwidth_for_noise() -> None:
    rng = np.random.default_rng(1234)
    samples = (
        rng.normal(0.0, 1.0, 8192)
        + 1j * rng.normal(0.0, 1.0, 8192)
    ).astype(np.complex64)

    narrow = _meter_for_bandwidth(10_000.0)._measure_channel_power(samples)
    wide = _meter_for_bandwidth(80_000.0)._measure_channel_power(samples)

    expected_delta_db = 10.0 * math.log10(8.0)
    assert abs((wide - narrow) - expected_delta_db) <= 1.5


def test_channel_power_1_to_100_khz_noise_delta() -> None:
    rng = np.random.default_rng(5678)
    samples = (
        rng.normal(0.0, 1.0, 65536)
        + 1j * rng.normal(0.0, 1.0, 65536)
    ).astype(np.complex64)
    narrow_meter = _meter_for_bandwidth(1_000.0)
    wide_meter = _meter_for_bandwidth(100_000.0)
    narrow_meter._sample_rate_hz = 1_000_000.0
    narrow_meter._bandwidth_hz = 1_536_000.0
    wide_meter._sample_rate_hz = 1_000_000.0
    wide_meter._bandwidth_hz = 1_536_000.0

    narrow = narrow_meter._measure_channel_power(samples)
    wide = wide_meter._measure_channel_power(samples)

    assert abs((wide - narrow) - 20.0) <= 2.0


def test_measurement_bandwidth_uses_gui_khz_parameter() -> None:
    assert _measurement_bandwidth_param_hz({"measurement_bandwidth_khz": 100.0}, 25_000.0) == 100_000.0
    assert _measurement_bandwidth_param_hz({"measurement_bandwidth_khz": 1.0}, 25_000.0) == 1_000.0


class DummySdr:
    def listSettings(self):
        return ["lnaState", "hdrMode"]

    def readSetting(self, *args):
        key = args[-1]
        if key == "lnaState":
            return "3"
        if key == "hdrMode":
            return "false"
        raise RuntimeError(key)


def test_read_first_setting_uses_sdr_readback() -> None:
    meter = SoapySdrplayLevelMeter()
    meter._sdr = DummySdr()
    meter._direction = 0
    meter._channel = 0

    assert meter._read_first_setting(("lnaState", "lnastate")) == "3"
    assert meter._read_first_setting(("hdrMode",)) == "false"
    assert meter._read_first_setting(("missing",)) is None


class FailingCloseSdr:
    def closeStream(self, stream):
        raise RuntimeError("native service disappeared")

    def deactivateStream(self, stream):
        raise RuntimeError("native service disappeared")


def test_close_tolerates_native_sdr_service_failure() -> None:
    meter = SoapySdrplayLevelMeter()
    meter._sdr = FailingCloseSdr()
    meter._stream = object()
    meter._stream_active = True

    meter.close()

    assert meter._sdr is None
    assert meter._stream is None
    assert meter._stream_active is False
