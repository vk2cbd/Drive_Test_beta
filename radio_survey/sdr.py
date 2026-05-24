from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SpectrumSnapshot:
    frequencies_mhz: tuple[float, ...]
    powers_dbm: tuple[float, ...]


class LevelMeter(Protocol):
    def configure(self, params: dict[str, object]) -> None: ...
    def update_settings(self, params: dict[str, object]) -> None: ...
    def read_level_dbm(self) -> float: ...
    def get_last_spectrum(self) -> SpectrumSnapshot | None: ...
    def close(self) -> None: ...


class SimulatedLevelMeter:
    def __init__(self) -> None:
        self._start = time.monotonic()
        self._offset = -82.0
        self._center_frequency_hz = 100_000_000.0
        self._bandwidth_hz = 1_536_000.0
        self._last_spectrum: SpectrumSnapshot | None = None

    def configure(self, params: dict[str, object]) -> None:
        self._offset = float(params.get("simulated_level_dbm", -82.0))
        self._center_frequency_hz = float(params.get("center_frequency_hz", self._center_frequency_hz))
        self._bandwidth_hz = float(params.get("bandwidth_hz", self._bandwidth_hz))

    def update_settings(self, params: dict[str, object]) -> None:
        self.configure(params)

    def read_level_dbm(self) -> float:
        elapsed = time.monotonic() - self._start
        slow_fade = math.sin(elapsed / 14.0) * 5.0
        flutter = math.sin(elapsed * 2.7) * 1.5
        noise = random.gauss(0.0, 0.8)
        level = self._offset + slow_fade + flutter + noise
        self._last_spectrum = self._make_simulated_spectrum(level, elapsed)
        return level

    def get_last_spectrum(self) -> SpectrumSnapshot | None:
        return self._last_spectrum

    def close(self) -> None:
        return

    def _make_simulated_spectrum(self, level_dbm: float, elapsed: float) -> SpectrumSnapshot:
        bins = 161
        start_hz = self._center_frequency_hz - self._bandwidth_hz / 2.0
        step_hz = self._bandwidth_hz / (bins - 1)
        carrier_offset = math.sin(elapsed / 12.0) * 0.18
        frequencies: list[float] = []
        powers: list[float] = []
        for index in range(bins):
            fraction = (index / (bins - 1)) * 2.0 - 1.0
            frequency_hz = start_hz + step_hz * index
            noise_floor = level_dbm - 38.0 + random.gauss(0.0, 1.8)
            signal = 28.0 * math.exp(-((fraction - carrier_offset) / 0.08) ** 2)
            shoulder = 9.0 * math.exp(-((fraction + 0.35) / 0.18) ** 2)
            frequencies.append(frequency_hz / 1_000_000.0)
            powers.append(noise_floor + signal + shoulder)
        return SpectrumSnapshot(tuple(frequencies), tuple(powers))


class SoapySdrplayLevelMeter:
    def __init__(self) -> None:
        self._sdr = None
        self._stream = None
        self._stream_active = False
        self._samples_per_level = 8192
        self._dbm_offset = -30.0
        self._last_level_dbm: float | None = None
        self._last_spectrum: SpectrumSnapshot | None = None
        self._center_frequency_hz = 100_000_000.0
        self._sample_rate_hz = 2_000_000.0
        self._bandwidth_hz = 1_536_000.0

    def configure(self, params: dict[str, object]) -> None:
        try:
            import numpy as np
            import SoapySDR
            from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
        except ImportError as exc:
            raise RuntimeError("SoapySDR and numpy are required for the soapy_sdrplay backend") from exc

        self._np = np
        self._SoapySDR = SoapySDR
        self._direction = SOAPY_SDR_RX
        self._channel = 0 if params.get("tuner", "A") == "A" else 1
        self._format = SOAPY_SDR_CF32
        self._transient_read_codes = {
            getattr(SoapySDR, "SOAPY_SDR_OVERFLOW", -4),
            getattr(SoapySDR, "SOAPY_SDR_TIMEOUT", -1),
        }
        self._samples_per_level = int(params.get("samples_per_level", 8192))
        self._dbm_offset = float(params.get("dbm_offset", -30.0))
        self._center_frequency_hz = float(params["center_frequency_hz"])
        self._sample_rate_hz = float(params["sample_rate_hz"])
        self._bandwidth_hz = float(params["bandwidth_hz"])

        device_args = _parse_device_args(str(params.get("device_args", "driver=sdrplay")))
        try:
            self._sdr = SoapySDR.Device(device_args)
        except Exception as exc:
            raise RuntimeError(
                "SoapySDR could not find an SDRplay device matching "
                f"{_format_device_args(device_args)}. On Ubuntu, run "
                "`SoapySDRUtil --find` and use the reported driver/device args, "
                "or switch the SDR backend to simulator until the SDRplay API and "
                "SoapySDR SDRplay module are installed."
            ) from exc
        self._sdr.setSampleRate(self._direction, self._channel, self._sample_rate_hz)
        self._sdr.setFrequency(self._direction, self._channel, self._center_frequency_hz)
        self._set_if_supported("setBandwidth", self._bandwidth_hz)
        self._set_antenna(str(params.get("antenna", "A")))
        self._set_gain(params)
        self._write_settings(params)
        self._stream = self._sdr.setupStream(self._direction, self._format, [self._channel])

    def update_settings(self, params: dict[str, object]) -> None:
        if self._sdr is None:
            raise RuntimeError("SDR is not configured")

        self._deactivate_stream()
        self._center_frequency_hz = float(params["center_frequency_hz"])
        self._sample_rate_hz = float(params["sample_rate_hz"])
        self._bandwidth_hz = float(params["bandwidth_hz"])
        self._samples_per_level = int(params.get("samples_per_level", self._samples_per_level))
        self._dbm_offset = float(params.get("dbm_offset", self._dbm_offset))
        self._sdr.setFrequency(self._direction, self._channel, self._center_frequency_hz)
        self._sdr.setSampleRate(self._direction, self._channel, self._sample_rate_hz)
        self._set_if_supported("setBandwidth", self._bandwidth_hz)
        self._set_antenna(str(params.get("antenna", "A")))
        self._set_gain(params)
        self._write_settings(params)

    def read_level_dbm(self) -> float:
        if self._sdr is None or self._stream is None:
            raise RuntimeError("SDR is not configured")

        buff = self._np.empty(self._samples_per_level, self._np.complex64)
        last_error_code: int | None = None
        self._activate_stream()

        try:
            for _attempt in range(10):
                result = self._sdr.readStream(self._stream, [buff], len(buff), timeoutUs=250_000)
                if result.ret > 0:
                    samples = buff[: result.ret]
                    rms = self._np.sqrt(self._np.mean(self._np.abs(samples) ** 2))
                    dbfs = 20.0 * math.log10(max(float(rms), 1e-12))
                    self._last_level_dbm = dbfs + self._dbm_offset
                    self._last_spectrum = self._make_spectrum(samples)
                    return self._last_level_dbm
                last_error_code = int(result.ret)
                if last_error_code not in self._transient_read_codes:
                    raise RuntimeError(f"SDR read failed with code {last_error_code}")
        finally:
            self._deactivate_stream()

        if self._last_level_dbm is not None:
            return self._last_level_dbm
        raise RuntimeError(
            "SDR did not return samples before timeout. Try a lower sample rate, "
            "larger samples-per-level value, or confirm the SDRplay API service is stable."
        )

    def get_last_spectrum(self) -> SpectrumSnapshot | None:
        return self._last_spectrum

    def close(self) -> None:
        if self._sdr is not None and self._stream is not None:
            try:
                self._deactivate_stream()
                self._sdr.closeStream(self._stream)
            finally:
                self._stream = None
                self._sdr = None
                self._stream_active = False

    def _activate_stream(self) -> None:
        if not self._stream_active:
            self._sdr.activateStream(self._stream)
            self._stream_active = True

    def _deactivate_stream(self) -> None:
        if self._stream_active:
            self._sdr.deactivateStream(self._stream)
            self._stream_active = False

    def _make_spectrum(self, samples: object) -> SpectrumSnapshot:
        fft_size = min(len(samples), 4096)
        if fft_size < 16:
            return SpectrumSnapshot((), ())

        samples = samples[-fft_size:]
        window = self._np.hanning(fft_size).astype(self._np.float32)
        spectrum = self._np.fft.fftshift(self._np.fft.fft(samples * window))
        offsets_hz = self._np.fft.fftshift(self._np.fft.fftfreq(fft_size, d=1.0 / self._sample_rate_hz))
        powers_dbm = 20.0 * self._np.log10(self._np.maximum(self._np.abs(spectrum) / fft_size, 1e-12)) + self._dbm_offset

        half_bw = self._bandwidth_hz / 2.0
        mask = self._np.abs(offsets_hz) <= half_bw
        if mask.any():
            offsets_hz = offsets_hz[mask]
            powers_dbm = powers_dbm[mask]

        frequencies_mhz = (self._center_frequency_hz + offsets_hz) / 1_000_000.0
        frequencies_mhz, powers_dbm = _thin_spectrum(frequencies_mhz, powers_dbm, 512)
        return SpectrumSnapshot(tuple(float(value) for value in frequencies_mhz), tuple(float(value) for value in powers_dbm))

    def _set_if_supported(self, method_name: str, value: object) -> None:
        method = getattr(self._sdr, method_name, None)
        if method is not None:
            method(self._direction, self._channel, value)

    def _set_antenna(self, antenna: str) -> None:
        if self._sdr is None:
            return

        requested_names = (antenna, f"Antenna {antenna}") if len(antenna) == 1 else (antenna,)
        try:
            available = self._sdr.listAntennas(self._direction, self._channel)
        except Exception:
            available = ()

        for name in requested_names:
            if not available or name in available:
                try:
                    self._sdr.setAntenna(self._direction, self._channel, name)
                    return
                except Exception:
                    continue

    def _set_gain(self, params: dict[str, object]) -> None:
        if str(params.get("gain_mode", "manual")) == "agc":
            self._sdr.setGainMode(self._direction, self._channel, True)
            return

        self._sdr.setGainMode(self._direction, self._channel, False)
        try:
            gain_names = set(self._sdr.listGains(self._direction, self._channel))
        except Exception:
            gain_names = {"RFGR", "IFGR"}
        for name, key in (("RFGR", "rf_gain_reduction_db"), ("IFGR", "if_gain_reduction_db")):
            if gain_names and name not in gain_names:
                continue
            try:
                self._sdr.setGain(self._direction, self._channel, name, float(params[key]))
            except Exception:
                pass

    def _write_settings(self, params: dict[str, object]) -> None:
        setting_map = {
            "lna_state": "lnaState",
            "bias_t": "biasT_ctrl",
            "rf_notch": "rfnotch_ctrl",
            "dab_notch": "dabnotch_ctrl",
            "ppm_correction": "corr",
        }
        for param_key, setting_key in setting_map.items():
            if param_key not in params:
                continue
            try:
                self._sdr.writeSetting(setting_key, str(params[param_key]))
            except Exception:
                pass


def create_level_meter(backend: str) -> LevelMeter:
    if backend == "soapy_sdrplay":
        return SoapySdrplayLevelMeter()
    return SimulatedLevelMeter()


def _parse_device_args(value: str) -> dict[str, str]:
    args: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        key, separator, raw_value = item.partition("=")
        if separator:
            args[key.strip()] = raw_value.strip()
    return args or {"driver": "sdrplay"}


def _format_device_args(args: dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in args.items())


def _thin_spectrum(frequencies: object, powers: object, max_points: int) -> tuple[object, object]:
    length = len(frequencies)
    if length <= max_points:
        return frequencies, powers
    step = max(1, length // max_points)
    return frequencies[::step], powers[::step]
