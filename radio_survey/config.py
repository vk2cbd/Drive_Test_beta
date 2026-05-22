from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FieldKind = Literal["text", "int", "float", "choice", "bool"]


@dataclass(frozen=True)
class ParameterDef:
    key: str
    label: str
    kind: FieldKind
    default: object
    choices: tuple[object, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    units: str = ""
    help_text: str = ""


SDR_PARAMETER_DEFS: tuple[ParameterDef, ...] = (
    ParameterDef("backend", "SDR backend", "choice", "simulator", ("simulator", "soapy_sdrplay")),
    ParameterDef("device_args", "Device args", "text", "driver=sdrplay"),
    ParameterDef("tuner", "Tuner", "choice", "A", ("A", "B")),
    ParameterDef("antenna", "Antenna", "choice", "A", ("A", "B", "Hi-Z", "50 ohm")),
    ParameterDef("center_frequency_hz", "Center frequency", "float", 100_000_000.0, minimum=1_000.0, units="Hz"),
    ParameterDef(
        "sample_rate_hz",
        "Sample rate",
        "choice",
        2_000_000,
        (62_500, 96_000, 192_000, 250_000, 384_000, 500_000, 768_000, 1_000_000, 1_536_000, 2_000_000, 5_000_000, 6_000_000, 7_000_000, 8_000_000),
        units="samples/s",
    ),
    ParameterDef(
        "bandwidth_hz",
        "IF bandwidth",
        "choice",
        1_536_000,
        (200_000, 300_000, 600_000, 1_536_000, 5_000_000, 6_000_000, 7_000_000, 8_000_000),
        units="Hz",
    ),
    ParameterDef("if_mode", "IF mode", "choice", "Zero IF", ("Zero IF", "Low IF 450 kHz", "Low IF 1.62 MHz", "Low IF 2.048 MHz")),
    ParameterDef("lo_mode", "LO mode", "choice", "Auto", ("Auto", "120 MHz", "144 MHz", "168 MHz")),
    ParameterDef("gain_mode", "Gain mode", "choice", "manual", ("manual", "agc")),
    ParameterDef("rf_gain_reduction_db", "RF gain reduction", "float", 20.0, minimum=0.0, maximum=66.0, units="dB"),
    ParameterDef("if_gain_reduction_db", "IF gain reduction", "float", 30.0, minimum=0.0, maximum=66.0, units="dB"),
    ParameterDef("lna_state", "LNA state", "int", 0, minimum=0, maximum=9),
    ParameterDef("hdr_mode", "RSPdx HDR mode", "bool", False),
    ParameterDef("bias_t", "Bias-T", "bool", False),
    ParameterDef("rf_notch", "RF notch", "bool", False),
    ParameterDef("dab_notch", "DAB notch", "bool", False),
    ParameterDef("fm_notch", "FM notch", "bool", False),
    ParameterDef("mw_notch", "MW notch", "bool", False),
    ParameterDef("dc_offset_correction", "DC offset correction", "bool", True),
    ParameterDef("iq_balance_correction", "IQ balance correction", "bool", True),
    ParameterDef("ppm_correction", "PPM correction", "float", 0.0, units="ppm"),
    ParameterDef("decimation", "Decimation", "choice", 1, (1, 2, 4, 8, 16, 32)),
    ParameterDef("samples_per_level", "Samples per level", "int", 8192, minimum=1024, maximum=262144),
    ParameterDef("dbm_offset", "dBm calibration offset", "float", -30.0, units="dB"),
)

