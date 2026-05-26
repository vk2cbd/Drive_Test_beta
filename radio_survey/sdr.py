from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SpectrumSnapshot:
    frequencies_mhz: tuple[float, ...]
    powers_dbm: tuple[float, ...]


@dataclass(frozen=True)
class MeterDiagnostics:
    applied: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class LevelMeter(Protocol):
    def configure(self, params: dict[str, object]) -> None: ...
    def update_settings(self, params: dict[str, object]) -> None: ...
    def read_level_dbm(self) -> float: ...
    def get_last_spectrum(self) -> SpectrumSnapshot | None: ...
    def get_diagnostics(self) -> MeterDiagnostics: ...
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

    def get_diagnostics(self) -> MeterDiagnostics:
        return MeterDiagnostics(applied=("Simulator level source",), warnings=())

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
        self._measurement_bandwidth_hz = 25_000.0
        self._diagnostics = MeterDiagnostics()
        self._reader_thread: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._condition = threading.Condition()
        self._last_error: str | None = None
        self._sample_counter = 0

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
        self._measurement_bandwidth_hz = _measurement_bandwidth_param_hz(params, 25_000.0)
        self._last_level_dbm = None
        self._last_spectrum = None
        self._sample_counter = 0
        notes: list[str] = []
        warnings: list[str] = []
        self._diagnostics = MeterDiagnostics()

        device_args = _normalize_device_args(str(params.get("device_args", "driver=sdrplay")))
        try:
            self._sdr = SoapySDR.Device(device_args)
        except Exception as exc:
            raise RuntimeError(
                "SoapySDR could not find an SDRplay device matching "
                f"{device_args}. On Ubuntu, run "
                "`SoapySDRUtil --find` and use the reported driver/device args, "
                "or switch the SDR backend to simulator until the SDRplay API and "
                "SoapySDR SDRplay module are installed."
            ) from exc
        self._sdr.setSampleRate(self._direction, self._channel, self._sample_rate_hz)
        self._sdr.setFrequency(self._direction, self._channel, self._center_frequency_hz)
        self._set_if_supported("setBandwidth", self._bandwidth_hz, warnings)
        notes.append(f"CF {self._center_frequency_hz / 1_000_000.0:.6f} MHz")
        notes.append(f"SR {self._sample_rate_hz / 1_000_000.0:g} Msps")
        notes.append(f"IF BW {self._bandwidth_hz / 1_000_000.0:g} MHz")
        notes.append(self._set_antenna(str(params.get("antenna", "A"))))
        notes.extend(self._set_gain(params, warnings))
        notes.extend(self._write_settings(params, warnings))
        notes.extend(self._actual_state_notes(params, warnings))
        self._stream = self._sdr.setupStream(self._direction, self._format, [self._channel])
        self._diagnostics = MeterDiagnostics(tuple(notes), tuple(warnings))
        self._start_reader()

    def update_settings(self, params: dict[str, object]) -> None:
        if self._sdr is None:
            raise RuntimeError("SDR is not configured")

        self._stop_reader_thread()
        notes: list[str] = []
        warnings: list[str] = []
        self._center_frequency_hz = float(params["center_frequency_hz"])
        self._sample_rate_hz = float(params["sample_rate_hz"])
        self._bandwidth_hz = float(params["bandwidth_hz"])
        self._samples_per_level = int(params.get("samples_per_level", self._samples_per_level))
        self._dbm_offset = float(params.get("dbm_offset", self._dbm_offset))
        self._measurement_bandwidth_hz = _measurement_bandwidth_param_hz(params, self._measurement_bandwidth_hz)
        with self._condition:
            self._last_level_dbm = None
            self._last_spectrum = None
            self._last_error = None
            self._sample_counter = 0
        self._sdr.setFrequency(self._direction, self._channel, self._center_frequency_hz)
        self._sdr.setSampleRate(self._direction, self._channel, self._sample_rate_hz)
        self._set_if_supported("setBandwidth", self._bandwidth_hz, warnings)
        notes.append(f"CF {self._center_frequency_hz / 1_000_000.0:.6f} MHz")
        notes.append(f"SR {self._sample_rate_hz / 1_000_000.0:g} Msps")
        notes.append(f"IF BW {self._bandwidth_hz / 1_000_000.0:g} MHz")
        notes.append(self._set_antenna(str(params.get("antenna", "A"))))
        notes.extend(self._set_gain(params, warnings))
        notes.extend(self._write_settings(params, warnings))
        notes.extend(self._actual_state_notes(params, warnings))
        self._diagnostics = MeterDiagnostics(tuple(notes), tuple(warnings))
        self._start_reader()

    def read_level_dbm(self) -> float:
        if self._sdr is None or self._stream is None:
            raise RuntimeError("SDR is not configured")

        with self._condition:
            start_counter = self._sample_counter
            if self._last_level_dbm is None:
                self._condition.wait(timeout=2.0)
            elif self._sample_counter == start_counter:
                self._condition.wait(timeout=0.25)
            if self._last_error:
                raise RuntimeError(self._last_error)
            if self._last_level_dbm is not None:
                return self._last_level_dbm
        raise RuntimeError("SDR is running but has not produced samples yet")

    def get_last_spectrum(self) -> SpectrumSnapshot | None:
        with self._condition:
            return self._last_spectrum

    def get_diagnostics(self) -> MeterDiagnostics:
        return self._diagnostics

    def close(self) -> None:
        self._stop_reader_thread()
        if self._sdr is not None and self._stream is not None:
            try:
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

    def _start_reader(self) -> None:
        if self._stream is None:
            return
        self._stop_reader.clear()
        self._last_error = None
        self._activate_stream()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="sdrplay-reader", daemon=True)
        self._reader_thread.start()

    def _stop_reader_thread(self) -> None:
        self._stop_reader.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        self._deactivate_stream()

    def _reader_loop(self) -> None:
        while not self._stop_reader.is_set():
            try:
                buff = self._np.empty(self._samples_per_level, self._np.complex64)
                result = self._sdr.readStream(self._stream, [buff], len(buff), timeoutUs=250_000)
                if result.ret > 0:
                    samples = buff[: result.ret].copy()
                    level_dbm = self._measure_channel_power(samples)
                    spectrum = self._make_spectrum(samples)
                    with self._condition:
                        self._last_level_dbm = level_dbm
                        self._last_spectrum = spectrum
                        self._last_error = None
                        self._sample_counter += 1
                        self._condition.notify_all()
                    continue

                code = int(result.ret)
                if code not in self._transient_read_codes:
                    with self._condition:
                        self._last_error = f"SDR read failed with code {code}"
                        self._condition.notify_all()
                    time.sleep(0.25)
            except Exception as exc:
                with self._condition:
                    self._last_error = f"SDR read failed: {exc}"
                    self._condition.notify_all()
                time.sleep(0.25)

    def _measure_channel_power(self, samples: object) -> float:
        fft_size = min(len(samples), 8192)
        if fft_size < 16:
            return self._last_level_dbm if self._last_level_dbm is not None else -140.0

        samples = samples[-fft_size:]
        window = self._np.hanning(fft_size).astype(self._np.float32)
        spectrum = self._np.fft.fftshift(self._np.fft.fft(samples * window))
        offsets_hz = self._np.fft.fftshift(self._np.fft.fftfreq(fft_size, d=1.0 / self._sample_rate_hz))
        half_measurement_bw = self._effective_measurement_bandwidth_hz() / 2.0
        mask = self._np.abs(offsets_hz) <= half_measurement_bw
        if not mask.any():
            mask = self._np.ones_like(offsets_hz, dtype=bool)
        window_power = max(float(self._np.sum(window ** 2)), 1.0)
        power_linear = (self._np.abs(spectrum[mask]) ** 2) / (float(fft_size) * window_power)
        dbfs = 10.0 * math.log10(max(float(self._np.sum(power_linear)), 1e-24))
        return dbfs + self._dbm_offset

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

    def _set_if_supported(self, method_name: str, value: object, warnings: list[str]) -> None:
        method = getattr(self._sdr, method_name, None)
        if method is not None:
            method(self._direction, self._channel, value)
        else:
            warnings.append(f"{method_name} unavailable")

    def _set_antenna(self, antenna: str) -> str:
        if self._sdr is None:
            return "Antenna unavailable"

        requested_names = (antenna, f"Antenna {antenna}") if len(antenna) == 1 else (antenna,)
        try:
            available = self._sdr.listAntennas(self._direction, self._channel)
        except Exception:
            available = ()

        for name in requested_names:
            if not available or name in available:
                try:
                    self._sdr.setAntenna(self._direction, self._channel, name)
                    actual = self._get_antenna()
                    return f"Antenna {actual or name}"
                except Exception:
                    continue
        available_text = ", ".join(str(value) for value in available) if available else "unknown"
        raise RuntimeError(f"Antenna {antenna} was not accepted by the SDR. Available antennas: {available_text}")

    def _set_gain(self, params: dict[str, object], warnings: list[str]) -> tuple[str, ...]:
        if str(params.get("gain_mode", "manual")) == "agc":
            self._sdr.setGainMode(self._direction, self._channel, True)
            return ("AGC on",)

        self._sdr.setGainMode(self._direction, self._channel, False)
        try:
            gain_names = set(self._sdr.listGains(self._direction, self._channel))
        except Exception:
            gain_names = {"RFGR", "IFGR"}
        applied: list[str] = ["AGC off"]
        for name, key in (("RFGR", "rf_gain_reduction_db"), ("IFGR", "if_gain_reduction_db")):
            if gain_names and name not in gain_names:
                warnings.append(f"{name} gain control unavailable")
                continue
            try:
                requested = float(params[key])
                self._sdr.setGain(self._direction, self._channel, name, requested)
                actual = self._get_gain(name)
                applied.append(f"{name} {actual if actual is not None else requested:g} dB")
            except Exception as exc:
                warnings.append(f"{name} not applied: {exc}")
        return tuple(applied)

    def _write_settings(self, params: dict[str, object], warnings: list[str]) -> tuple[str, ...]:
        applied: list[str] = []
        setting_map = {
            "lna_state": ("lnaState", "lnastate", "lna_state"),
            "hdr_mode": ("hdrMode", "hdrmode", "rspdx_hdr", "hdr_ctrl"),
            "bias_t": ("biasT_ctrl", "biasT", "bias_t"),
            "dab_notch": ("dabnotch_ctrl", "dabNotch", "dab_notch"),
            "mw_notch": ("mwnotch_ctrl", "mwNotch", "mw_notch"),
            "if_mode": ("if_mode", "ifMode", "IF_Mode"),
            "lo_mode": ("lo_mode", "loMode", "LO_Mode"),
            "decimation": ("decimation", "decimationFactor"),
        }
        for param_key, setting_keys in setting_map.items():
            if param_key not in params:
                continue
            applied_key = self._write_first_setting(setting_keys, params[param_key], warnings)
            if applied_key:
                applied.append(f"{param_key} via {applied_key}")
        if "fm_notch" in params:
            applied_key = self._write_first_setting(("rfnotch_ctrl", "rfNotch", "rf_notch"), params["fm_notch"], warnings)
            if applied_key:
                applied.append(f"fm_notch via {applied_key}")
        if "ppm_correction" in params:
            ppm = float(params["ppm_correction"])
            method = getattr(self._sdr, "setFrequencyCorrection", None)
            if method is not None:
                try:
                    method(self._direction, self._channel, ppm)
                    applied.append(f"PPM {ppm:g}")
                except Exception as exc:
                    warnings.append(f"PPM correction not applied: {exc}")
            else:
                applied_key = self._write_first_setting(("corr", "ppm", "ppm_correction"), ppm, warnings)
                if applied_key:
                    applied.append(f"PPM via {applied_key}")
        correction_methods_applied: set[str] = set()
        for param_key, method_name in (
            ("dc_offset_correction", "setDCOffsetMode"),
            ("iq_balance_correction", "setIQBalanceMode"),
        ):
            if param_key not in params:
                continue
            method = getattr(self._sdr, method_name, None)
            if method is None:
                warnings.append(f"{param_key} unavailable")
                continue
            try:
                method(self._direction, self._channel, bool(params[param_key]))
                applied.append(param_key)
                correction_methods_applied.add(param_key)
            except Exception as exc:
                warnings.append(f"{param_key} not applied: {exc}")
        if "iq_balance_correction" in params and "iq_balance_correction" not in correction_methods_applied:
            applied_key = self._write_first_setting(("iqcorr_ctrl", "iqcorr", "iq_balance_correction"), params["iq_balance_correction"], warnings)
            if applied_key:
                applied.append(f"iq_balance_correction via {applied_key}")
        applied.append(
            f"Power BW {self._measurement_bandwidth_hz / 1_000.0:g} kHz "
            f"(effective {self._effective_measurement_bandwidth_hz() / 1_000.0:g} kHz)"
        )
        return tuple(applied)

    def _actual_state_notes(self, params: dict[str, object], warnings: list[str]) -> tuple[str, ...]:
        notes: list[str] = ["Actual SDR state"]
        antenna = self._get_antenna()
        if antenna is not None:
            notes.append(f"antenna={antenna}")
        gain_mode = self._get_gain_mode()
        if gain_mode is not None:
            notes.append(f"agc={'on' if gain_mode else 'off'}")
        for gain_name in ("RFGR", "IFGR"):
            gain = self._get_gain(gain_name)
            if gain is not None:
                notes.append(f"{gain_name}={gain:g} dB")
        setting_map = {
            "lna": ("lnaState", "lnastate", "lna_state"),
            "hdr": ("hdrMode", "hdrmode", "rspdx_hdr", "hdr_ctrl"),
            "biasT": ("biasT_ctrl", "biasT", "bias_t"),
            "dabNotch": ("dabnotch_ctrl", "dabNotch", "dab_notch"),
            "fmNotch": ("rfnotch_ctrl", "rfNotch", "rf_notch"),
            "mwNotch": ("mwnotch_ctrl", "mwNotch", "mw_notch"),
            "ifMode": ("if_mode", "ifMode", "IF_Mode"),
            "loMode": ("lo_mode", "loMode", "LO_Mode"),
            "decimation": ("decimation", "decimationFactor"),
        }
        for label, keys in setting_map.items():
            value = self._read_first_setting(keys)
            if value is not None:
                notes.append(f"{label}={value}")
        if len(notes) == 1:
            warnings.append("No SDR readback settings were available")
            return ()
        return tuple(notes)

    def _effective_measurement_bandwidth_hz(self) -> float:
        return max(1.0, min(self._measurement_bandwidth_hz, self._bandwidth_hz, self._sample_rate_hz))

    def _write_first_setting(self, keys: tuple[str, ...], value: object, warnings: list[str]) -> str | None:
        available = self._available_setting_keys()
        candidates = keys if not available else tuple(key for key in keys if key in available)
        if not candidates:
            warnings.append(f"{keys[0]} setting unavailable from SoapySDRPlay3")
            return None
        text_value = _setting_value(value)
        for key in candidates:
            try:
                self._sdr.writeSetting(key, text_value)
                return key
            except Exception:
                continue
        warnings.append(f"{keys[0]} setting was rejected")
        return None

    def _available_setting_keys(self) -> set[str]:
        try:
            settings = self._sdr.listSettings()
        except Exception:
            return set()
        keys: set[str] = set()
        for setting in settings:
            key = getattr(setting, "key", None)
            if key is None and isinstance(setting, str):
                key = setting
            if key:
                keys.add(str(key))
        return keys

    def _get_antenna(self) -> str | None:
        try:
            return str(self._sdr.getAntenna(self._direction, self._channel))
        except Exception:
            return None

    def _get_gain(self, name: str) -> float | None:
        try:
            return float(self._sdr.getGain(self._direction, self._channel, name))
        except Exception:
            return None

    def _get_gain_mode(self) -> bool | None:
        try:
            return bool(self._sdr.getGainMode(self._direction, self._channel))
        except Exception:
            return None

    def _read_first_setting(self, keys: tuple[str, ...]) -> str | None:
        available = self._available_setting_keys()
        candidates = keys if not available else tuple(key for key in keys if key in available)
        for key in candidates:
            try:
                return str(self._sdr.readSetting(self._direction, self._channel, key))
            except Exception:
                try:
                    return str(self._sdr.readSetting(key))
                except Exception:
                    continue
        return None


def create_level_meter(backend: str) -> LevelMeter:
    if backend == "soapy_sdrplay":
        return SoapySdrplayLevelMeter()
    return SimulatedLevelMeter()


def _normalize_device_args(value: str) -> str:
    args = _parse_device_args(value)
    return _format_device_args(args)


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


def _setting_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _measurement_bandwidth_param_hz(params: dict[str, object], default_hz: float) -> float:
    if "measurement_bandwidth_khz" in params:
        return float(params["measurement_bandwidth_khz"]) * 1_000.0
    if "measurement_bandwidth_hz" in params:
        return float(params["measurement_bandwidth_hz"])
    return default_hz
