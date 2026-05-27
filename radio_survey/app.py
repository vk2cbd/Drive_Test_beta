from __future__ import annotations

import queue
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .calibration import (
    CALIBRATION_BANDS,
    CalibrationBand,
    CalibrationPoint,
    CalibrationProfile,
    calibration_band_for_label,
    load_calibrations,
    new_calibration_profile,
    save_calibration,
)
from .config import SDR_PARAMETER_DEFS, ParameterDef
from .gps import SerialGpsSource, SimulatedGpsSource, default_gps_port, discover_gps_ports
from .logger import CsvSurveyLogger
from .nmea import GpsFix
from .settings import load_settings, save_settings
from .sdr import LevelMeter, create_level_meter


@dataclass(frozen=True)
class LevelPoint:
    epoch_s: float
    level_dbm: float


@dataclass(frozen=True)
class PlotViewState:
    zoom: tuple[float, float, float, float] | None
    window_minutes: int
    y_min: float
    y_max: float
    autoscale_y: bool


CALIBRATION_TARGETS: tuple[tuple[str, float | None], ...] = (
    ("Noise floor", None),
    ("-100 dBm", -100.0),
    ("-80 dBm", -80.0),
    ("-60 dBm", -60.0),
    ("-40 dBm", -40.0),
    ("1 dB compression", None),
)
GPS_FIX_STALE_SECONDS = 3.0


class SurveyApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Radio Network Survey Logger")
        self.geometry("1280x920")
        self.minsize(1180, 900)

        self._gps_source = None
        self._level_meter: LevelMeter | None = None
        self._active_sdr_backend: str | None = None
        self._logger: CsvSurveyLogger | None = None
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._points: list[LevelPoint] = []
        self._vars: dict[str, tk.Variable] = {}
        self._widgets: dict[str, ttk.Widget] = {}
        self._settings = load_settings()
        self._last_valid_sdr_values: dict[str, object] = {}
        self._last_valid_y_max = self._valid_y_value(self._settings.get("plot_y_max_dbm", -40.0), -40.0)
        self._last_valid_y_min = self._valid_y_value(self._settings.get("plot_y_min_dbm", -120.0), -120.0)
        if self._last_valid_y_max <= self._last_valid_y_min:
            self._last_valid_y_min = -120.0
            self._last_valid_y_max = -40.0
        self._last_valid_spectrum_y_max = self._valid_y_value(self._settings.get("spectrum_y_max_dbm", -20.0), -20.0)
        self._last_valid_spectrum_y_min = self._valid_spectrum_y_value(self._settings.get("spectrum_y_min_dbm", -150.0), -150.0)
        if float(self._last_valid_spectrum_y_min) == -120.0:
            self._last_valid_spectrum_y_min = -150.0
        if self._last_valid_spectrum_y_max <= self._last_valid_spectrum_y_min:
            self._last_valid_spectrum_y_min = -150.0
            self._last_valid_spectrum_y_max = -20.0
        self._last_valid_spectrum_averages = self._valid_spectrum_averages(self._settings.get("spectrum_averages", 1), 1)
        self._spectrum_average_powers: tuple[float, ...] | None = None
        self._spectrum_average_frequencies: tuple[float, ...] | None = None
        self._calibrations: dict[str, CalibrationProfile] = load_calibrations()
        self._calibration: CalibrationProfile | None = None
        self._calibration_valid = False
        self._last_valid_plot_window_minutes = self._valid_plot_window_minutes(self._settings.get("plot_window_minutes", 10))
        self._last_autoscale_y = False
        self._plot_right_edge_s: float | None = None
        self._plot_zoom: tuple[float, float, float, float] | None = None
        self._plot_view_history: list[PlotViewState] = []
        self._plot_drag_start: tuple[float, float] | None = None
        self._plot_drag_current: tuple[float, float] | None = None
        self._plot_drag_rect: int | None = None
        self._last_plot_bounds: tuple[float, float, float, float, float, float, float, float] | None = None
        self._running = False
        self._last_measurement_signature: tuple[object, ...] | None = None
        self._last_sampled_gps_second: tuple[int, int, int] | None = None
        self._last_fix_monotonic_s: float | None = None
        self._survey_started_monotonic_s: float | None = None
        self._gps_stale_reported = False
        self._gps_serial_error_active = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        setup_container = ttk.Frame(self)
        setup_container.grid(row=0, column=0, sticky="nsw")
        setup_container.rowconfigure(0, weight=1)
        setup_container.columnconfigure(0, weight=1)

        setup_canvas = tk.Canvas(setup_container, width=430, highlightthickness=0)
        setup_scrollbar = ttk.Scrollbar(setup_container, orient="vertical", command=setup_canvas.yview)
        setup_canvas.configure(yscrollcommand=setup_scrollbar.set)
        setup_canvas.grid(row=0, column=0, sticky="ns")
        setup_scrollbar.grid(row=0, column=1, sticky="ns")

        setup = ttk.Frame(setup_canvas, padding=10)
        setup.columnconfigure(1, weight=1)
        setup_window = setup_canvas.create_window((0, 0), window=setup, anchor="nw")
        setup.bind("<Configure>", lambda event: setup_canvas.configure(scrollregion=setup_canvas.bbox("all")))
        setup_canvas.bind("<Configure>", lambda event: setup_canvas.itemconfigure(setup_window, width=event.width))
        setup_canvas.bind_all("<MouseWheel>", lambda event: setup_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))
        setup_canvas.bind_all("<Button-4>", lambda _event: setup_canvas.yview_scroll(-1, "units"))
        setup_canvas.bind_all("<Button-5>", lambda _event: setup_canvas.yview_scroll(1, "units"))

        plot_area = ttk.Frame(self, padding=(0, 10, 10, 10))
        plot_area.grid(row=0, column=1, sticky="nsew")
        plot_area.columnconfigure(0, weight=1)
        plot_area.columnconfigure(1, weight=1)
        plot_area.rowconfigure(2, weight=0)
        plot_area.rowconfigure(4, weight=1)

        self._build_io_panel(setup)
        self._build_calibration_panel(setup)
        self._build_status_panel(plot_area)
        self._build_plot_panel(plot_area)
        self._build_sdr_panel(plot_area)
        self._update_calibration_status()

        self.title(f"Radio Network Survey Logger {__version__}")

    def _build_io_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Survey IO", padding=8)
        frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        self.gps_sim_var = tk.BooleanVar(value=bool(self._settings.get("gps_simulated", False)))
        ttk.Checkbutton(frame, text="Simulated GPS", variable=self.gps_sim_var, command=self._commit_all_settings).grid(row=0, column=0, columnspan=3, sticky="w")

        self.gps_port_var = tk.StringVar(value=str(self._settings.get("gps_port", default_gps_port())))
        self.gps_baud_var = tk.IntVar(value=int(self._settings.get("gps_baud", 4800)))
        ttk.Label(frame, text="GPS port").grid(row=1, column=0, sticky="w")
        self.gps_port_combo = ttk.Combobox(frame, textvariable=self.gps_port_var, values=discover_gps_ports(), width=18)
        self.gps_port_combo.grid(row=1, column=1, sticky="ew", padx=4)
        self.gps_port_combo.bind("<<ComboboxSelected>>", lambda _event: self._commit_all_settings())
        self.gps_port_combo.bind("<Return>", lambda _event: self._commit_all_settings())
        self.gps_port_combo.bind("<KP_Enter>", lambda _event: self._commit_all_settings())
        ttk.Button(frame, text="Refresh", command=self._refresh_gps_ports).grid(row=1, column=2, sticky="e")
        ttk.Label(frame, text="Baud").grid(row=2, column=0, sticky="w")
        gps_baud_entry = ttk.Entry(frame, textvariable=self.gps_baud_var, width=12)
        gps_baud_entry.grid(row=2, column=1, sticky="ew", padx=4)
        gps_baud_entry.bind("<Return>", lambda _event: self._commit_all_settings())
        gps_baud_entry.bind("<KP_Enter>", lambda _event: self._commit_all_settings())

        self.csv_path_var = tk.StringVar(value=str(self._settings.get("csv_path", Path.home() / "radio_survey_logs" / "survey_log.csv")))
        ttk.Label(frame, text="CSV file").grid(row=3, column=0, sticky="w")
        csv_entry = ttk.Entry(frame, textvariable=self.csv_path_var, width=32)
        csv_entry.grid(row=3, column=1, sticky="ew", padx=4)
        csv_entry.bind("<Return>", lambda _event: self._commit_all_settings())
        csv_entry.bind("<KP_Enter>", lambda _event: self._commit_all_settings())
        ttk.Button(frame, text="Browse", command=self._browse_csv).grid(row=3, column=2, sticky="e")

        self.start_button = ttk.Button(frame, text="Start", command=self._start)
        self.start_button.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.stop_button = ttk.Button(frame, text="Stop", command=self._stop, state="disabled")
        self.stop_button.grid(row=5, column=1, sticky="ew", padx=4, pady=(8, 0))

        self.logging_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Log to CSV", variable=self.logging_enabled_var, command=self._toggle_logging).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.autoscale_y_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Autoscale Y", variable=self.autoscale_y_var, command=self._commit_autoscale_y).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.y_max_var = tk.StringVar(value=f"{self._last_valid_y_max:.0f}")
        self.y_min_var = tk.StringVar(value=f"{self._last_valid_y_min:.0f}")
        ttk.Label(frame, text="Y max").grid(row=8, column=0, sticky="w")
        y_max_entry = ttk.Entry(frame, textvariable=self.y_max_var, width=8)
        y_max_entry.grid(row=8, column=1, sticky="ew", padx=4)
        y_max_entry.bind("<Return>", lambda _event: self._commit_y_axis())
        y_max_buttons = ttk.Frame(frame)
        y_max_buttons.grid(row=8, column=2, sticky="e")
        ttk.Button(y_max_buttons, text="-", width=2, command=lambda: self._step_y_axis(self.y_max_var, -5.0)).grid(row=0, column=0)
        ttk.Button(y_max_buttons, text="+", width=2, command=lambda: self._step_y_axis(self.y_max_var, 5.0)).grid(row=0, column=1)

        ttk.Label(frame, text="Y min").grid(row=9, column=0, sticky="w")
        y_min_entry = ttk.Entry(frame, textvariable=self.y_min_var, width=8)
        y_min_entry.grid(row=9, column=1, sticky="ew", padx=4)
        y_min_entry.bind("<Return>", lambda _event: self._commit_y_axis())
        y_min_buttons = ttk.Frame(frame)
        y_min_buttons.grid(row=9, column=2, sticky="e")
        ttk.Button(y_min_buttons, text="-", width=2, command=lambda: self._step_y_axis(self.y_min_var, -5.0)).grid(row=0, column=0)
        ttk.Button(y_min_buttons, text="+", width=2, command=lambda: self._step_y_axis(self.y_min_var, 5.0)).grid(row=0, column=1)

        self.autoscale_spectrum_y_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Autoscale Spectrum Y", variable=self.autoscale_spectrum_y_var, command=self._redraw_spectrum).grid(row=10, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.spectrum_y_max_var = tk.StringVar(value=f"{self._last_valid_spectrum_y_max:.0f}")
        self.spectrum_y_min_var = tk.StringVar(value=f"{self._last_valid_spectrum_y_min:.0f}")
        ttk.Label(frame, text="Spec Y max").grid(row=11, column=0, sticky="w")
        spectrum_y_max_entry = ttk.Entry(frame, textvariable=self.spectrum_y_max_var, width=8)
        spectrum_y_max_entry.grid(row=11, column=1, sticky="ew", padx=4)
        spectrum_y_max_entry.bind("<Return>", lambda _event: self._commit_spectrum_y_axis())
        spectrum_y_max_buttons = ttk.Frame(frame)
        spectrum_y_max_buttons.grid(row=11, column=2, sticky="e")
        ttk.Button(spectrum_y_max_buttons, text="-", width=2, command=lambda: self._step_spectrum_y_axis(self.spectrum_y_max_var, -5.0)).grid(row=0, column=0)
        ttk.Button(spectrum_y_max_buttons, text="+", width=2, command=lambda: self._step_spectrum_y_axis(self.spectrum_y_max_var, 5.0)).grid(row=0, column=1)

        ttk.Label(frame, text="Spec Y min").grid(row=12, column=0, sticky="w")
        spectrum_y_min_entry = ttk.Entry(frame, textvariable=self.spectrum_y_min_var, width=8)
        spectrum_y_min_entry.grid(row=12, column=1, sticky="ew", padx=4)
        spectrum_y_min_entry.bind("<Return>", lambda _event: self._commit_spectrum_y_axis())
        spectrum_y_min_buttons = ttk.Frame(frame)
        spectrum_y_min_buttons.grid(row=12, column=2, sticky="e")
        ttk.Button(spectrum_y_min_buttons, text="-", width=2, command=lambda: self._step_spectrum_y_axis(self.spectrum_y_min_var, -5.0)).grid(row=0, column=0)
        ttk.Button(spectrum_y_min_buttons, text="+", width=2, command=lambda: self._step_spectrum_y_axis(self.spectrum_y_min_var, 5.0)).grid(row=0, column=1)

        self.spectrum_averages_var = tk.StringVar(value=str(self._last_valid_spectrum_averages))
        ttk.Label(frame, text="Spec averages").grid(row=13, column=0, sticky="w")
        spectrum_averages_entry = ttk.Entry(frame, textvariable=self.spectrum_averages_var, width=8)
        spectrum_averages_entry.grid(row=13, column=1, sticky="ew", padx=4)
        spectrum_averages_entry.bind("<Return>", lambda _event: self._commit_spectrum_averages())

    def _build_calibration_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Calibration", padding=8)
        frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        self.calibration_point_var = tk.StringVar(value=CALIBRATION_TARGETS[1][0])
        self.calibration_compression_var = tk.StringVar(value=str(self._settings.get("compression_input_dbm", "-30.0")))
        band_labels = [band.label for band in CALIBRATION_BANDS]
        saved_band_label = str(self._settings.get("calibration_band_label", CALIBRATION_BANDS[0].label))
        self.calibration_band_var = tk.StringVar(value=saved_band_label if saved_band_label in band_labels else CALIBRATION_BANDS[0].label)
        self.calibration_locked_var = tk.BooleanVar(value=False)
        self.calibration_status_var = tk.StringVar(value="No calibration loaded")

        ttk.Label(frame, text="Band").grid(row=0, column=0, sticky="w")
        band_combo = ttk.Combobox(
            frame,
            textvariable=self.calibration_band_var,
            values=band_labels,
            state="readonly",
            width=24,
        )
        band_combo.grid(row=0, column=1, sticky="ew", padx=4)
        band_combo.bind("<<ComboboxSelected>>", lambda _event: self._select_calibration_band())

        ttk.Label(frame, text="Point").grid(row=1, column=0, sticky="w")
        point_combo = ttk.Combobox(
            frame,
            textvariable=self.calibration_point_var,
            values=[label for label, _target in CALIBRATION_TARGETS],
            state="readonly",
            width=18,
        )
        point_combo.grid(row=1, column=1, sticky="ew", padx=4)

        ttk.Label(frame, text="Compression dBm").grid(row=2, column=0, sticky="w")
        compression_entry = ttk.Entry(frame, textvariable=self.calibration_compression_var, width=12)
        compression_entry.grid(row=2, column=1, sticky="ew", padx=4)
        compression_entry.bind("<Return>", lambda _event: self._save_current_settings())
        compression_entry.bind("<KP_Enter>", lambda _event: self._save_current_settings())

        ttk.Checkbutton(frame, text="Locked", variable=self.calibration_locked_var, command=self._toggle_calibration_lock).grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Button(frame, text="New band cal", command=self._new_calibration).grid(row=3, column=1, sticky="ew", padx=4, pady=(6, 0))
        ttk.Button(frame, text="Capture point", command=self._capture_calibration_point).grid(row=4, column=1, sticky="ew", padx=4, pady=(6, 0))

        self.calibration_status_label = tk.Label(frame, textvariable=self.calibration_status_var, anchor="w", justify="left", wraplength=360)
        self.calibration_status_label.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _build_sdr_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="SDR Parameters", padding=8)
        frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        rows_per_group = 9
        for group in range(3):
            frame.columnconfigure(group * 3 + 1, weight=1)

        for index, param in enumerate(SDR_PARAMETER_DEFS):
            group = index // rows_per_group
            row = index % rows_per_group
            column = group * 3
            ttk.Label(frame, text=param.label).grid(row=row, column=column, sticky="w", pady=1, padx=(0 if group == 0 else 12, 0))
            var = self._make_var(param)
            self._vars[param.key] = var
            widget = self._make_widget(frame, param, var)
            self._widgets[param.key] = widget
            widget.grid(row=row, column=column + 1, sticky="ew", padx=4, pady=1)
            self._bind_commit(widget, param)
            self._last_valid_sdr_values[param.key] = self._coerce_param_value(param, var.get())
            if param.units:
                ttk.Label(frame, text=param.units).grid(row=row, column=column + 2, sticky="w")

        ttk.Label(frame, text="SDR readback").grid(row=rows_per_group, column=0, sticky="nw", pady=(8, 0))
        ttk.Label(frame, textvariable=self.sdr_status_var, wraplength=920, justify="left").grid(
            row=rows_per_group,
            column=1,
            columnspan=8,
            sticky="ew",
            padx=4,
            pady=(8, 0),
        )

    def _build_status_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Realtime Data", padding=10)
        frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        for index in range(4):
            frame.columnconfigure(index, weight=1)

        self.status_var = tk.StringVar(value="Idle")
        self.sdr_status_var = tk.StringVar(value="SDR not started")
        self.position_var = tk.StringVar(value="No fix")
        self.timestamp_var = tk.StringVar(value="-")
        self.date_var = tk.StringVar(value="-")
        self.fix_quality_var = tk.StringVar(value="-")
        self.satellites_var = tk.StringVar(value="-")
        self.speed_var = tk.StringVar(value="-")
        self.level_var = tk.StringVar(value="-")

        for column, (label, var) in enumerate(
            (
                ("Status", self.status_var),
                ("Position", self.position_var),
                ("GPS time", self.timestamp_var),
                ("Level", self.level_var),
            )
        ):
            ttk.Label(frame, text=label).grid(row=0, column=column, sticky="w")
            ttk.Label(frame, textvariable=var, font=("TkDefaultFont", 11, "bold")).grid(row=1, column=column, sticky="w")

        for column, (label, var) in enumerate(
            (
                ("Satellites", self.satellites_var),
                ("Fix quality", self.fix_quality_var),
                ("Date", self.date_var),
                ("Speed km/h", self.speed_var),
            )
        ):
            ttk.Label(frame, text=label).grid(row=2, column=column, sticky="w", pady=(8, 0))
            ttk.Label(frame, textvariable=var, font=("TkDefaultFont", 11, "bold")).grid(row=3, column=column, sticky="w")

    def _build_plot_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Spectrum (dBm)").grid(row=1, column=0, sticky="w", pady=(10, 2))
        ttk.Label(parent, text="Received Level (dBm)").grid(row=1, column=1, sticky="w", pady=(10, 2), padx=(10, 0))
        self.spectrum_canvas = tk.Canvas(parent, background="#101418", highlightthickness=0)
        self.spectrum_canvas.configure(height=260)
        self.spectrum_canvas.grid(row=2, column=0, sticky="nsew")
        self.spectrum_canvas.bind("<Configure>", lambda _event: self._redraw_spectrum())
        self.canvas = tk.Canvas(parent, background="#101418", highlightthickness=0)
        self.canvas.configure(height=260)
        self.canvas.grid(row=2, column=1, sticky="nsew", padx=(10, 0))
        self.canvas.bind("<Configure>", lambda _event: self._redraw_plot())
        self.canvas.bind("<ButtonPress-1>", self._plot_drag_start_event)
        self.canvas.bind("<B1-Motion>", self._plot_drag_motion_event)
        self.canvas.bind("<ButtonRelease-1>", self._plot_drag_release_event)

        controls = ttk.Frame(parent)
        controls.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
        controls.columnconfigure(1, weight=1)
        self.window_minutes_var = tk.IntVar(value=self._last_valid_plot_window_minutes)
        ttk.Label(controls, text="Plot window").grid(row=0, column=0, sticky="w")
        ttk.Scale(controls, from_=1, to=60, variable=self.window_minutes_var, command=lambda _v: self._commit_plot_window()).grid(row=0, column=1, sticky="ew", padx=4)
        window_spinbox = ttk.Spinbox(controls, from_=1, to=60, textvariable=self.window_minutes_var, width=5, command=self._commit_plot_window)
        window_spinbox.grid(row=0, column=2, sticky="e")
        window_spinbox.bind("<Return>", lambda _event: self._commit_plot_window())
        window_spinbox.bind("<KP_Enter>", lambda _event: self._commit_plot_window())
        ttk.Button(controls, text="Back", command=self._back_plot_view).grid(row=0, column=3, sticky="e", padx=(6, 0))

    def _make_var(self, param: ParameterDef) -> tk.Variable:
        value = self._settings.get(param.key, self._legacy_setting_value(param))
        if param.kind == "choice" and param.choices and value not in param.choices and str(value) not in {str(choice) for choice in param.choices}:
            value = param.default
        if param.kind == "bool":
            return tk.BooleanVar(value=bool(value))
        if param.kind == "int":
            return tk.IntVar(value=int(value))
        if param.kind == "float":
            return tk.StringVar(value=f"{float(value):.6f}" if param.key == "center_frequency_mhz" else str(float(value)))
        return tk.StringVar(value=str(value))

    def _make_widget(self, parent: ttk.Frame, param: ParameterDef, var: tk.Variable) -> ttk.Widget:
        if param.kind == "choice":
            return ttk.Combobox(parent, textvariable=var, values=[str(choice) for choice in param.choices], state="readonly")
        if param.kind == "bool":
            return ttk.Checkbutton(parent, variable=var)
        return ttk.Entry(parent, textvariable=var, width=18)

    def _bind_commit(self, widget: ttk.Widget, param: ParameterDef) -> None:
        if param.kind == "choice":
            widget.bind("<<ComboboxSelected>>", lambda _event: self._commit_all_settings())
            widget.bind("<Return>", lambda _event: self._commit_all_settings())
            widget.bind("<KP_Enter>", lambda _event: self._commit_all_settings())
        elif param.kind == "bool":
            widget.configure(command=self._commit_all_settings)
        else:
            widget.bind("<Return>", lambda _event: self._commit_all_settings())
            widget.bind("<KP_Enter>", lambda _event: self._commit_all_settings())

    def _browse_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Choose survey CSV",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if path:
            self.csv_path_var.set(path)
            self._commit_all_settings()

    def _refresh_gps_ports(self) -> None:
        ports = discover_gps_ports()
        self.gps_port_combo.configure(values=ports)
        if ports and self.gps_port_var.get() not in ports:
            self.gps_port_var.set(ports[0])
            self._commit_all_settings()

    def _start(self) -> None:
        if self._running:
            return
        try:
            self._save_current_settings()
            params = self._collect_sdr_params()
            self._level_meter = create_level_meter(str(params["backend"]))
            self._level_meter.configure(params)
            self._active_sdr_backend = str(params["backend"])
            self._last_measurement_signature = self._measurement_signature(params)
            self._refresh_sdr_status()
            if self.logging_enabled_var.get():
                self._open_logger()
            self._gps_source = SimulatedGpsSource() if self.gps_sim_var.get() else SerialGpsSource(self.gps_port_var.get(), int(self.gps_baud_var.get()))
            self._gps_source.start(self._queue_fix, self._queue_error)
        except Exception as exc:
            self._cleanup()
            messagebox.showerror("Unable to start survey", str(exc))
            return

        self._running = True
        self._last_sampled_gps_second = None
        self._last_fix_monotonic_s = None
        self._survey_started_monotonic_s = time.monotonic()
        self._gps_stale_reported = False
        self._gps_serial_error_active = False
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._clear_stale_realtime_fields()
        self.status_var.set("Running")

    def _stop(self) -> None:
        self._cleanup()
        self._running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("Stopped")
        self.sdr_status_var.set("SDR not started")
        self._plot_right_edge_s = time.time()
        self._redraw_plot()

    def _cleanup(self) -> None:
        if self._gps_source is not None:
            self._gps_source.stop()
        if self._level_meter is not None:
            self._level_meter.close()
        if self._logger is not None:
            self._logger.close()
        self._gps_source = None
        self._level_meter = None
        self._active_sdr_backend = None
        self._last_measurement_signature = None
        self._last_sampled_gps_second = None
        self._last_fix_monotonic_s = None
        self._survey_started_monotonic_s = None
        self._gps_stale_reported = False
        self._gps_serial_error_active = False
        self._logger = None

    def _commit_all_settings(self) -> None:
        try:
            self._format_frequency_field()
            if not self._validate_sdr_fields():
                return
            self._save_current_settings()
            if self._running:
                self._restart_gps_source()
                self._reconfigure_level_meter()
                self._sync_logger_state()
            else:
                self._update_calibration_status()
            self.status_var.set("Settings applied")
        except Exception as exc:
            self.status_var.set(f"Settings not applied: {exc}")

    def _commit_plot_window(self) -> None:
        previous_window = self._last_valid_plot_window_minutes
        window_minutes = self._valid_plot_window_minutes(self.window_minutes_var.get())
        self.window_minutes_var.set(window_minutes)
        if self._plot_zoom is not None or window_minutes != previous_window:
            self._push_plot_view_history(window_minutes=previous_window)
        self._plot_zoom = None
        self._last_valid_plot_window_minutes = window_minutes
        if self._points:
            self._plot_right_edge_s = self._points[-1].epoch_s
        self._redraw_plot()
        self._settings["plot_window_minutes"] = window_minutes
        save_settings(self._settings)

    def _commit_y_axis(self, redraw: bool = True) -> None:
        previous_y_min = self._last_valid_y_min
        previous_y_max = self._last_valid_y_max
        if not self._apply_y_axis_fields():
            self.status_var.set("Y axis values must be between -120 and -10 dBm")
            return
        if previous_y_min != self._last_valid_y_min or previous_y_max != self._last_valid_y_max:
            self._push_plot_view_history(y_min=previous_y_min, y_max=previous_y_max)
        self._settings["plot_y_max_dbm"] = self._last_valid_y_max
        self._settings["plot_y_min_dbm"] = self._last_valid_y_min
        save_settings(self._settings)
        if redraw:
            self._redraw_plot()

    def _commit_autoscale_y(self) -> None:
        autoscale_y = bool(self.autoscale_y_var.get())
        if autoscale_y != self._last_autoscale_y:
            self._push_plot_view_history(autoscale_y=self._last_autoscale_y)
            self._last_autoscale_y = autoscale_y
        self._redraw_plot()

    def _commit_spectrum_y_axis(self, redraw: bool = True) -> None:
        if not self._apply_spectrum_y_axis_fields():
            self.status_var.set("Spectrum Y axis values must be between -150 and -10 dBm")
            return
        self._settings["spectrum_y_max_dbm"] = self._last_valid_spectrum_y_max
        self._settings["spectrum_y_min_dbm"] = self._last_valid_spectrum_y_min
        save_settings(self._settings)
        if redraw:
            self._redraw_spectrum()

    def _commit_spectrum_averages(self) -> None:
        value = self._parse_spectrum_averages(self.spectrum_averages_var.get())
        if value is None:
            self.spectrum_averages_var.set(str(self._last_valid_spectrum_averages))
            self.status_var.set("Spectrum averages must be an integer from 1 to 100")
            return
        if value != self._last_valid_spectrum_averages:
            self._spectrum_average_powers = None
            self._spectrum_average_frequencies = None
        self._last_valid_spectrum_averages = value
        self.spectrum_averages_var.set(str(value))
        self._settings["spectrum_averages"] = value
        save_settings(self._settings)
        self._redraw_spectrum()

    def _step_y_axis(self, var: tk.StringVar, delta_db: float) -> None:
        current = self._parse_y_value(var.get())
        if current is None:
            current = self._last_valid_y_max if var is self.y_max_var else self._last_valid_y_min
        var.set(f"{self._clamp_y_value(current + delta_db):.0f}")
        self._commit_y_axis()

    def _step_spectrum_y_axis(self, var: tk.StringVar, delta_db: float) -> None:
        current = self._parse_spectrum_y_value(var.get())
        if current is None:
            current = self._last_valid_spectrum_y_max if var is self.spectrum_y_max_var else self._last_valid_spectrum_y_min
        var.set(f"{self._clamp_spectrum_y_value(current + delta_db):.0f}")
        self._commit_spectrum_y_axis()

    def _apply_y_axis_fields(self) -> bool:
        y_max = self._parse_y_value(self.y_max_var.get())
        y_min = self._parse_y_value(self.y_min_var.get())
        if y_max is None or y_min is None or y_max <= y_min:
            return False
        self._last_valid_y_max = y_max
        self._last_valid_y_min = y_min
        self.y_max_var.set(f"{y_max:.0f}")
        self.y_min_var.set(f"{y_min:.0f}")
        return True

    def _apply_spectrum_y_axis_fields(self) -> bool:
        y_max = self._parse_spectrum_y_value(self.spectrum_y_max_var.get())
        y_min = self._parse_spectrum_y_value(self.spectrum_y_min_var.get())
        if y_max is None or y_min is None or y_max <= y_min:
            return False
        self._last_valid_spectrum_y_max = y_max
        self._last_valid_spectrum_y_min = y_min
        self.spectrum_y_max_var.set(f"{y_max:.0f}")
        self.spectrum_y_min_var.set(f"{y_min:.0f}")
        return True

    def _parse_y_value(self, value: object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError, tk.TclError):
            return None
        if parsed < -120.0 or parsed > -10.0:
            return None
        return parsed

    def _valid_y_value(self, value: object, default: float) -> float:
        parsed = self._parse_y_value(value)
        return parsed if parsed is not None else default

    def _valid_plot_window_minutes(self, value: object) -> int:
        try:
            parsed = int(float(str(value).strip()))
        except (TypeError, ValueError, tk.TclError):
            return 10
        return min(60, max(1, parsed))

    def _clamp_y_value(self, value: float) -> float:
        return min(-10.0, max(-120.0, value))

    def _parse_spectrum_y_value(self, value: object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError, tk.TclError):
            return None
        if parsed < -150.0 or parsed > -10.0:
            return None
        return parsed

    def _valid_spectrum_y_value(self, value: object, default: float) -> float:
        parsed = self._parse_spectrum_y_value(value)
        return parsed if parsed is not None else default

    def _clamp_spectrum_y_value(self, value: float) -> float:
        return min(-10.0, max(-150.0, value))

    def _parse_spectrum_averages(self, value: object) -> int | None:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError, tk.TclError):
            return None
        if parsed < 1 or parsed > 100:
            return None
        return parsed

    def _valid_spectrum_averages(self, value: object, default: int) -> int:
        parsed = self._parse_spectrum_averages(value)
        return parsed if parsed is not None else default

    def _save_current_settings(self) -> None:
        settings = {
            "gps_simulated": self.gps_sim_var.get(),
            "gps_port": self.gps_port_var.get(),
            "gps_baud": int(self.gps_baud_var.get()),
            "csv_path": self.csv_path_var.get(),
            "plot_window_minutes": self._last_valid_plot_window_minutes,
            "plot_y_max_dbm": self._last_valid_y_max,
            "plot_y_min_dbm": self._last_valid_y_min,
            "spectrum_y_max_dbm": self._last_valid_spectrum_y_max,
            "spectrum_y_min_dbm": self._last_valid_spectrum_y_min,
            "spectrum_averages": self._last_valid_spectrum_averages,
            "compression_input_dbm": self.calibration_compression_var.get(),
            "calibration_band_label": self.calibration_band_var.get(),
        }
        settings.update(self._collect_sdr_display_params())
        self._settings = settings
        save_settings(settings)

    def _collect_sdr_display_params(self) -> dict[str, object]:
        params: dict[str, object] = {}
        definitions = {param.key: param for param in SDR_PARAMETER_DEFS}
        for key, var in self._vars.items():
            param = definitions[key]
            params[key] = self._coerce_param_value(param, var.get())
        return params

    def _restart_gps_source(self) -> None:
        if self._gps_source is not None:
            self._gps_source.stop()
        self._gps_source = SimulatedGpsSource() if self.gps_sim_var.get() else SerialGpsSource(self.gps_port_var.get(), int(self.gps_baud_var.get()))
        self._gps_source.start(self._queue_fix, self._queue_error)

    def _reconfigure_level_meter(self) -> None:
        params = self._collect_sdr_params()
        measurement_signature = self._measurement_signature(params)
        backend = str(params["backend"])
        if self._level_meter is not None and self._active_sdr_backend == backend:
            self._level_meter.update_settings(params)
            self._last_measurement_signature = measurement_signature
            self._refresh_sdr_status()
            self._update_calibration_status()
            return

        if self._level_meter is not None:
            self._level_meter.close()
        new_meter = create_level_meter(backend)
        new_meter.configure(params)
        self._level_meter = new_meter
        self._active_sdr_backend = backend
        self._last_measurement_signature = measurement_signature
        self._refresh_sdr_status()
        self._update_calibration_status()

    def _measurement_signature(self, params: dict[str, object]) -> tuple[object, ...]:
        return (
            params.get("center_frequency_hz"),
            params.get("sample_rate_hz"),
            params.get("bandwidth_hz"),
            params.get("measurement_bandwidth_khz"),
            params.get("dbm_offset"),
        )

    def _refresh_sdr_status(self) -> None:
        if self._level_meter is None:
            self.sdr_status_var.set("SDR not started")
            return
        diagnostics = self._level_meter.get_diagnostics()
        applied = "; ".join(diagnostics.applied) if diagnostics.applied else "No applied settings reported"
        if diagnostics.warnings:
            applied = f"{applied} | Warnings: {'; '.join(diagnostics.warnings)}"
        self.sdr_status_var.set(applied)
        self._update_calibration_status()

    def _new_calibration(self) -> None:
        band = self._selected_calibration_band()
        existing = self._calibrations.get(band.key)
        if existing is not None and existing.locked:
            self.status_var.set("Calibration is locked; unlock before starting a new band calibration")
            self._update_calibration_status()
            return
        try:
            self._format_frequency_field()
            if not self._validate_sdr_fields():
                return
            metadata = self._collect_calibration_metadata()
        except Exception as exc:
            self.status_var.set(f"Calibration not started: {exc}")
            return
        self._calibration = new_calibration_profile(band.key, metadata)
        self._calibrations[band.key] = self._calibration
        save_calibration(self._calibration)
        self.status_var.set(f"New {band.label} calibration started")
        self._update_calibration_status()

    def _capture_calibration_point(self) -> None:
        if self._level_meter is None:
            self.status_var.set("Start the survey before capturing calibration points")
            return
        if self._calibration is None:
            self._new_calibration()
            if self._calibration is None:
                return
        if self._calibration.locked:
            self.status_var.set("Calibration is locked; unlock before capturing points")
            self._update_calibration_status()
            return
        label = self.calibration_point_var.get()
        try:
            measured = self._level_meter.read_level_dbm()
            input_dbm = self._calibration_target_for_label(label)
        except Exception as exc:
            self.status_var.set(f"Calibration point not captured: {exc}")
            return
        point = CalibrationPoint(label=label, input_dbm=input_dbm, measured_dbm=measured)
        self._calibration = self._calibration.upsert_point(point)
        self._calibrations[self._calibration.band_key] = self._calibration
        save_calibration(self._calibration)
        self.status_var.set(f"Captured {label}: measured {measured:.2f} dBm")
        self._update_calibration_status()

    def _selected_calibration_band(self) -> CalibrationBand:
        return calibration_band_for_label(self.calibration_band_var.get())

    def _select_calibration_band(self) -> None:
        band = self._selected_calibration_band()
        self._calibration = self._calibrations.get(band.key)
        self._settings["calibration_band_label"] = band.label
        save_settings(self._settings)
        self._update_calibration_status()

    def _toggle_calibration_lock(self) -> None:
        band = self._selected_calibration_band()
        profile = self._calibrations.get(band.key)
        if profile is None:
            self.calibration_locked_var.set(False)
            self.status_var.set("No calibration loaded for this band")
            return
        requested = bool(self.calibration_locked_var.get())
        if profile.locked and not requested:
            if not messagebox.askyesno("Unlock calibration", f"Unlock {band.label} calibration for editing?"):
                self.calibration_locked_var.set(True)
                return
        profile = profile.with_locked(requested)
        self._calibrations[band.key] = profile
        self._calibration = profile
        save_calibration(profile)
        self.status_var.set(f"{band.label} calibration {'locked' if requested else 'unlocked'}")
        self._update_calibration_status()

    def _calibration_target_for_label(self, label: str) -> float | None:
        if label == "1 dB compression":
            return float(self.calibration_compression_var.get())
        for target_label, target in CALIBRATION_TARGETS:
            if target_label == label:
                return target
        return None

    def _collect_calibration_metadata(self) -> dict[str, object]:
        metadata = self._collect_sdr_display_params()
        metadata["center_frequency_mhz"] = round(float(metadata.get("center_frequency_mhz", 0.0)), 6)
        return metadata

    def _update_calibration_status(self) -> None:
        if not hasattr(self, "calibration_status_var"):
            return
        band = self._selected_calibration_band()
        self._calibration = self._calibrations.get(band.key)
        if self._calibration is None:
            self._calibration_valid = False
            self.calibration_locked_var.set(False)
            self.calibration_status_var.set(f"No {band.label} calibration loaded")
            self.calibration_status_label.configure(fg="#555555")
            return
        self.calibration_locked_var.set(self._calibration.locked)
        mismatches = self._calibration.metadata_mismatches(self._collect_calibration_metadata())
        point_count = len(self._calibration.points)
        if mismatches:
            self._calibration_valid = False
            self.calibration_status_var.set(
                f"{band.label} mismatch: {', '.join(mismatches[:4])}"
                + ("..." if len(mismatches) > 4 else "")
            )
            self.calibration_status_label.configure(fg="#b00020")
        else:
            self._calibration_valid = self._calibration.has_points(tuple(label for label, _target in CALIBRATION_TARGETS))
            state = "active" if self._calibration_valid else "loaded"
            locked = ", locked" if self._calibration.locked else ", unlocked"
            self.calibration_status_var.set(f"{band.label} calibration {state}: {point_count}/6 points{locked}")
            self.calibration_status_label.configure(fg="#222222")

    def _toggle_logging(self) -> None:
        self._sync_logger_state()

    def _sync_logger_state(self) -> None:
        if not self._running:
            return
        if self.logging_enabled_var.get():
            self._open_logger()
        elif self._logger is not None:
            self._logger.close()
            self._logger = None

    def _open_logger(self) -> None:
        if self._logger is not None:
            return
        self._logger = CsvSurveyLogger(self.csv_path_var.get())
        self._logger.open()

    def _format_frequency_field(self) -> None:
        if "center_frequency_mhz" in self._vars:
            try:
                value = float(self._vars["center_frequency_mhz"].get())
            except (TypeError, ValueError, tk.TclError):
                return
            self._vars["center_frequency_mhz"].set(f"{value:.6f}")

    def _validate_sdr_fields(self) -> bool:
        for param in SDR_PARAMETER_DEFS:
            var = self._vars[param.key]
            try:
                value = self._coerce_param_value(param, var.get())
            except (TypeError, ValueError, tk.TclError):
                self._restore_sdr_field(param)
                self.status_var.set(f"{param.label} was not applied: invalid value")
                return False
            if not self._param_value_in_range(param, value):
                self._restore_sdr_field(param)
                self.status_var.set(
                    f"{param.label} was not applied: valid range is {param.minimum} to {param.maximum}"
                )
                return False
            self._last_valid_sdr_values[param.key] = value
            self._set_param_display_value(param, value)
        return True

    def _restore_sdr_field(self, param: ParameterDef) -> None:
        self._set_param_display_value(param, self._last_valid_sdr_values.get(param.key, param.default))

    def _set_param_display_value(self, param: ParameterDef, value: object) -> None:
        if param.kind == "bool":
            self._vars[param.key].set(bool(value))
        elif param.kind == "int":
            self._vars[param.key].set(str(int(value)))
        elif param.kind == "float":
            if param.key == "center_frequency_mhz":
                self._vars[param.key].set(f"{float(value):.6f}")
            else:
                self._vars[param.key].set(str(float(value)))
        else:
            self._vars[param.key].set(str(value))

    def _param_value_in_range(self, param: ParameterDef, value: object) -> bool:
        if param.kind == "choice" and param.choices:
            return str(value) in {str(choice) for choice in param.choices}
        if param.kind not in ("int", "float"):
            return True
        numeric = float(value)
        if param.minimum is not None and numeric < param.minimum:
            return False
        if param.maximum is not None and numeric > param.maximum:
            return False
        return True

    def _coerce_param_value(self, param: ParameterDef, value: object) -> object:
        if param.kind == "bool":
            return bool(value)
        if param.kind == "int":
            return int(value)
        if param.kind == "float":
            return float(value)
        if param.kind == "choice":
            return self._coerce_choice(value)
        return str(value)

    def _collect_sdr_params(self) -> dict[str, object]:
        params: dict[str, object] = {}
        definitions = {param.key: param for param in SDR_PARAMETER_DEFS}
        for key, var in self._vars.items():
            param = definitions[key]
            params[key] = self._coerce_param_value(param, var.get())
        if "center_frequency_mhz" in params:
            params["center_frequency_hz"] = float(params.pop("center_frequency_mhz")) * 1_000_000.0
        if "sample_rate_msps" in params:
            params["sample_rate_hz"] = float(params.pop("sample_rate_msps")) * 1_000_000.0
        if "bandwidth_mhz" in params:
            params["bandwidth_hz"] = float(params.pop("bandwidth_mhz")) * 1_000_000.0
        return params

    def _legacy_setting_value(self, param: ParameterDef) -> object:
        if param.key == "sample_rate_msps" and "sample_rate_hz" in self._settings:
            return float(self._settings["sample_rate_hz"]) / 1_000_000.0
        if param.key == "bandwidth_mhz" and "bandwidth_hz" in self._settings:
            return float(self._settings["bandwidth_hz"]) / 1_000_000.0
        return param.default

    def _coerce_choice(self, value: object) -> object:
        text = str(value)
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return text

    def _queue_fix(self, fix: GpsFix) -> None:
        self._events.put(("fix", fix))

    def _queue_error(self, message: str) -> None:
        self._events.put(("error", message))

    def _process_events(self) -> None:
        while True:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if event == "fix":
                self._handle_fix(payload)
            elif event == "error":
                self._handle_gps_error(str(payload))
        self._check_gps_stale()
        self.after(100, self._process_events)

    def _handle_fix(self, fix: GpsFix) -> None:
        if not isinstance(fix, GpsFix) or self._level_meter is None:
            return
        self._last_fix_monotonic_s = time.monotonic()
        self._gps_stale_reported = False
        self._gps_serial_error_active = False
        self._update_gps_display(fix)
        gps_second = _gps_second_key(fix.timestamp_utc)
        if gps_second == self._last_sampled_gps_second:
            return
        try:
            raw_level = self._level_meter.read_level_dbm()
            level = self._apply_calibration(raw_level)
            if self.logging_enabled_var.get() and self._logger is not None:
                self._logger.write(fix, level)
        except Exception as exc:
            self.status_var.set(str(exc))
            return

        self._last_sampled_gps_second = gps_second
        self.level_var.set(f"{level:.1f} dBm")
        point_time = time.time()
        self._points.append(LevelPoint(point_time, level))
        if self._plot_zoom is None:
            self._plot_right_edge_s = point_time
        self._update_spectrum_average()
        self._redraw_spectrum()
        self._redraw_plot()

    def _check_gps_stale(self) -> None:
        if not self._running or self._gps_stale_reported or self._gps_serial_error_active:
            return
        if self._last_fix_monotonic_s is None:
            started_s = self._survey_started_monotonic_s or time.monotonic()
            if time.monotonic() - started_s < GPS_FIX_STALE_SECONDS:
                return
        elif time.monotonic() - self._last_fix_monotonic_s < GPS_FIX_STALE_SECONDS:
            return
        self._clear_stale_realtime_fields()
        self.status_var.set("GPS fix lost")
        self._gps_stale_reported = True

    def _handle_gps_error(self, message: str) -> None:
        self._gps_serial_error_active = True
        self._clear_stale_realtime_fields()
        self.status_var.set(message)

    def _clear_stale_realtime_fields(self) -> None:
        self.position_var.set("No fix")
        self.timestamp_var.set("-")
        self.date_var.set("-")
        self.fix_quality_var.set("-")
        self.satellites_var.set("-")
        self.speed_var.set("-")
        self.level_var.set("-")

    def _update_gps_display(self, fix: GpsFix) -> None:
        local_timestamp = fix.timestamp_utc.astimezone()
        self.position_var.set(fix.position_dms)
        self.timestamp_var.set(local_timestamp.strftime("%H:%M:%S %Z"))
        self.date_var.set(local_timestamp.strftime("%Y-%m-%d"))
        if fix.quality is not None:
            self.fix_quality_var.set(str(fix.quality))
        if fix.satellites is not None:
            self.satellites_var.set(str(fix.satellites))
        if fix.speed_kmh is not None:
            self.speed_var.set(f"{fix.speed_kmh:.1f}")

    def _redraw_plot(self) -> None:
        canvas = self.canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        self._plot_drag_rect = None

        margin_left = 54
        margin_right = 14
        margin_top = 16
        margin_bottom = 34
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom

        canvas.create_rectangle(0, 0, width, height, fill="#101418", outline="")
        canvas.create_rectangle(margin_left, margin_top, width - margin_right, height - margin_bottom, outline="#39424d")

        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = margin_top + plot_h * fraction
            canvas.create_line(margin_left, y, width - margin_right, y, fill="#8fa2b3")
        for fraction in (0.25, 0.5, 0.75):
            x = margin_left + plot_w * fraction
            canvas.create_line(x, margin_top, x, height - margin_bottom, fill="#8fa2b3")

        if not self._points:
            self._last_plot_bounds = None
            self._plot_drag_start = None
            self._plot_drag_current = None
            self._plot_drag_rect = None
            canvas.create_text(width / 2, height / 2, text="Waiting for GPS fixes", fill="#b7c0c9", font=("TkDefaultFont", 13))
            return

        x_min, x_max, low, high = self._current_plot_view()
        visible = [point for point in self._points if x_min <= point.epoch_s <= x_max]
        if not visible:
            self._last_plot_bounds = (margin_left, margin_top, plot_w, plot_h, x_min, x_max, low, high)
            self._draw_plot_drag_overlay()
            return

        if self._plot_zoom is None and self.autoscale_y_var.get():
            min_level = min(point.level_dbm for point in visible)
            max_level = max(point.level_dbm for point in visible)
            span = max(max_level - min_level, 10.0)
            low = min_level - (span * 0.1)
            high = max_level + (span * 0.1)
        elif self._plot_zoom is None:
            self._apply_y_axis_fields()
            low = self._last_valid_y_min
            high = self._last_valid_y_max

        self._last_plot_bounds = (margin_left, margin_top, plot_w, plot_h, x_min, x_max, low, high)

        for label_value in (low, (low + high) / 2, high):
            y = margin_top + (high - label_value) / (high - low) * plot_h
            canvas.create_text(margin_left - 8, y, text=f"{label_value:.0f}", fill="#b7c0c9", anchor="e", font=("TkDefaultFont", 9))

        coords: list[float] = []
        for point in visible:
            x = margin_left + (point.epoch_s - x_min) / max(x_max - x_min, 1e-6) * plot_w
            y = margin_top + (high - point.level_dbm) / (high - low) * plot_h
            coords.extend((x, y))

        if len(coords) >= 4:
            canvas.create_line(*coords, fill="#5cc8ff", width=2, smooth=True)
        x, y = coords[-2], coords[-1]
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#ffffff", outline="#5cc8ff")
        canvas.create_text(margin_left, height - 16, text=_format_axis_time(x_min), fill="#b7c0c9", anchor="w")
        canvas.create_text(width - margin_right, height - 16, text=_format_axis_time(x_max), fill="#b7c0c9", anchor="e")
        self._draw_plot_drag_overlay()

    def _current_plot_view(self) -> tuple[float, float, float, float]:
        if self._plot_zoom is not None:
            return self._plot_zoom
        right_edge = self._plot_right_edge_s or self._points[-1].epoch_s
        window_s = max(60, self._last_valid_plot_window_minutes * 60)
        x_min = right_edge - window_s
        x_max = right_edge
        self._apply_y_axis_fields()
        return x_min, x_max, self._last_valid_y_min, self._last_valid_y_max

    def _capture_plot_view_state(
        self,
        *,
        window_minutes: int | None = None,
        y_min: float | None = None,
        y_max: float | None = None,
        autoscale_y: bool | None = None,
    ) -> PlotViewState:
        return PlotViewState(
            zoom=self._plot_zoom,
            window_minutes=self._last_valid_plot_window_minutes if window_minutes is None else window_minutes,
            y_min=self._last_valid_y_min if y_min is None else y_min,
            y_max=self._last_valid_y_max if y_max is None else y_max,
            autoscale_y=self._last_autoscale_y if autoscale_y is None else autoscale_y,
        )

    def _push_plot_view_history(
        self,
        *,
        window_minutes: int | None = None,
        y_min: float | None = None,
        y_max: float | None = None,
        autoscale_y: bool | None = None,
    ) -> None:
        previous_view = self._capture_plot_view_state(
            window_minutes=window_minutes,
            y_min=y_min,
            y_max=y_max,
            autoscale_y=autoscale_y,
        )
        if self._plot_view_history and self._plot_view_history[-1] == previous_view:
            return
        self._plot_view_history.append(previous_view)
        del self._plot_view_history[:-20]

    def _back_plot_view(self) -> None:
        if not self._plot_view_history:
            return
        view = self._plot_view_history.pop()
        self._plot_zoom = view.zoom
        self._last_valid_plot_window_minutes = view.window_minutes
        self.window_minutes_var.set(view.window_minutes)
        self._last_valid_y_min = view.y_min
        self._last_valid_y_max = view.y_max
        self.y_min_var.set(f"{view.y_min:.0f}")
        self.y_max_var.set(f"{view.y_max:.0f}")
        self._last_autoscale_y = view.autoscale_y
        self.autoscale_y_var.set(view.autoscale_y)
        self._settings["plot_window_minutes"] = view.window_minutes
        self._settings["plot_y_min_dbm"] = view.y_min
        self._settings["plot_y_max_dbm"] = view.y_max
        save_settings(self._settings)
        if self._plot_zoom is None and self._points:
            self._plot_right_edge_s = self._points[-1].epoch_s
        self._redraw_plot()

    def _plot_drag_start_event(self, event: tk.Event) -> None:
        if self._last_plot_bounds is None:
            return
        x = self._clamp_plot_x(float(event.x))
        y = self._clamp_plot_y(float(event.y))
        self._plot_drag_start = (x, y)
        self._plot_drag_current = (x, y)
        self._draw_plot_drag_overlay()

    def _plot_drag_motion_event(self, event: tk.Event) -> None:
        if self._plot_drag_start is None:
            return
        x1 = self._clamp_plot_x(float(event.x))
        y1 = self._clamp_plot_y(float(event.y))
        self._plot_drag_current = (x1, y1)
        self._draw_plot_drag_overlay()

    def _plot_drag_release_event(self, event: tk.Event) -> None:
        if self._plot_drag_start is None or self._last_plot_bounds is None:
            return
        x0, y0 = self._plot_drag_start
        x1 = self._clamp_plot_x(float(event.x))
        y1 = self._clamp_plot_y(float(event.y))
        self._plot_drag_start = None
        self._plot_drag_current = None
        self._delete_plot_drag_overlay()
        self._plot_drag_rect = None
        if abs(x1 - x0) < 8 or abs(y1 - y0) < 8:
            return
        self._push_plot_view_history()
        self._plot_zoom = self._pixel_rect_to_plot_range(x0, y0, x1, y1)
        self._redraw_plot()

    def _draw_plot_drag_overlay(self) -> None:
        if self._plot_drag_start is None or self._plot_drag_current is None:
            return
        x0, y0 = self._plot_drag_start
        x1, y1 = self._plot_drag_current
        if self._plot_drag_rect is None:
            self._plot_drag_rect = self.canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                outline="#ffd24d",
                width=3,
                dash=(5, 2),
            )
        else:
            try:
                self.canvas.coords(self._plot_drag_rect, x0, y0, x1, y1)
            except tk.TclError:
                self._plot_drag_rect = None
                self._draw_plot_drag_overlay()
                return
        self.canvas.lift(self._plot_drag_rect)

    def _delete_plot_drag_overlay(self) -> None:
        if self._plot_drag_rect is None:
            return
        try:
            self.canvas.delete(self._plot_drag_rect)
        except tk.TclError:
            pass
        self._plot_drag_rect = None

    def _clamp_plot_x(self, value: float) -> float:
        if self._last_plot_bounds is None:
            return value
        margin_left, _margin_top, plot_w, _plot_h, *_rest = self._last_plot_bounds
        return min(margin_left + plot_w, max(margin_left, value))

    def _clamp_plot_y(self, value: float) -> float:
        if self._last_plot_bounds is None:
            return value
        _margin_left, margin_top, _plot_w, plot_h, *_rest = self._last_plot_bounds
        return min(margin_top + plot_h, max(margin_top, value))

    def _pixel_rect_to_plot_range(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        margin_left, margin_top, plot_w, plot_h, x_min, x_max, y_min, y_max = self._last_plot_bounds
        left_px, right_px = sorted((x0, x1))
        top_px, bottom_px = sorted((y0, y1))
        new_x_min = x_min + (left_px - margin_left) / plot_w * (x_max - x_min)
        new_x_max = x_min + (right_px - margin_left) / plot_w * (x_max - x_min)
        new_y_max = y_max - (top_px - margin_top) / plot_h * (y_max - y_min)
        new_y_min = y_max - (bottom_px - margin_top) / plot_h * (y_max - y_min)
        return new_x_min, new_x_max, new_y_min, new_y_max

    def _redraw_spectrum(self) -> None:
        canvas = self.spectrum_canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")

        margin_left = 58
        margin_right = 18
        margin_top = 16
        margin_bottom = 38
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom

        canvas.create_rectangle(0, 0, width, height, fill="#101418", outline="")
        canvas.create_rectangle(margin_left, margin_top, width - margin_right, height - margin_bottom, outline="#d7e0ea")

        frequencies = self._spectrum_average_frequencies
        powers = self._spectrum_average_powers
        if frequencies is None or powers is None or not frequencies or not powers:
            canvas.create_text(width / 2, height / 2, text="Waiting for spectrum", fill="#d7e0ea", font=("TkDefaultFont", 13))
            return
        x_min = min(frequencies)
        x_max = max(frequencies)
        if x_max <= x_min:
            return

        if self.autoscale_spectrum_y_var.get():
            min_power = min(powers)
            max_power = max(powers)
            span = max(max_power - min_power, 10.0)
            low = min_power - (span * 0.1)
            high = max_power + (span * 0.1)
        else:
            self._apply_spectrum_y_axis_fields()
            low = self._last_valid_spectrum_y_min
            high = self._last_valid_spectrum_y_max

        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = margin_top + plot_h * fraction
            x = margin_left + plot_w * fraction
            canvas.create_line(margin_left, y, width - margin_right, y, fill="#8fa2b3")
            canvas.create_line(x, margin_top, x, height - margin_bottom, fill="#8fa2b3")
            power_label = high - (high - low) * fraction
            freq_label = x_min + (x_max - x_min) * fraction
            canvas.create_text(margin_left - 8, y, text=f"{power_label:.0f}", fill="#d7e0ea", anchor="e", font=("TkDefaultFont", 9))
            canvas.create_text(x, height - 18, text=f"{freq_label:.3f}", fill="#d7e0ea", anchor="n", font=("TkDefaultFont", 9))

        coords: list[float] = []
        for frequency, power in zip(frequencies, powers):
            x = margin_left + (frequency - x_min) / (x_max - x_min) * plot_w
            clipped_power = max(low, min(high, power))
            y = margin_top + (high - clipped_power) / (high - low) * plot_h
            coords.extend((x, y))

        if len(coords) >= 4:
            canvas.create_line(*coords, fill="#f7d35c", width=1)
        canvas.create_text(width - margin_right, height - 4, text="MHz", fill="#d7e0ea", anchor="se", font=("TkDefaultFont", 9))

    def _update_spectrum_average(self) -> None:
        spectrum = self._level_meter.get_last_spectrum() if self._level_meter is not None else None
        if spectrum is None or not spectrum.frequencies_mhz or not spectrum.powers_dbm:
            return

        averages = self._last_valid_spectrum_averages
        powers = tuple(self._apply_calibration(float(value)) for value in spectrum.powers_dbm)
        if (
            self._spectrum_average_powers is None
            or self._spectrum_average_frequencies != spectrum.frequencies_mhz
            or len(self._spectrum_average_powers) != len(powers)
            or averages <= 1
        ):
            self._spectrum_average_frequencies = spectrum.frequencies_mhz
            self._spectrum_average_powers = powers
            return

        alpha = 1.0 / averages
        self._spectrum_average_powers = tuple(
            previous + alpha * (current - previous)
            for previous, current in zip(self._spectrum_average_powers, powers)
        )

    def _on_close(self) -> None:
        self._cleanup()
        self.destroy()

    def _apply_calibration(self, measured_dbm: float) -> float:
        if self._calibration_valid and self._calibration is not None:
            return self._calibration.apply(measured_dbm)
        return measured_dbm


def main() -> None:
    app = SurveyApp()
    app.mainloop()


def _format_local_time(timestamp_utc: datetime) -> str:
    return timestamp_utc.astimezone().strftime("%H:%M:%S %Z")


def _gps_second_key(timestamp_utc: datetime) -> tuple[int, int, int]:
    return timestamp_utc.hour, timestamp_utc.minute, timestamp_utc.second


def _format_axis_time(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s).strftime("%H:%M:%S")
