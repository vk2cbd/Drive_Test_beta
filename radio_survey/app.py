from __future__ import annotations

import queue
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
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
        self._last_valid_spectrum_y_min = self._valid_y_value(self._settings.get("spectrum_y_min_dbm", -120.0), -120.0)
        if self._last_valid_spectrum_y_max <= self._last_valid_spectrum_y_min:
            self._last_valid_spectrum_y_min = -120.0
            self._last_valid_spectrum_y_max = -20.0
        self._last_valid_spectrum_averages = self._valid_spectrum_averages(self._settings.get("spectrum_averages", 1), 1)
        self._spectrum_average_powers: tuple[float, ...] | None = None
        self._spectrum_average_frequencies: tuple[float, ...] | None = None
        self._running = False

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
        plot_area.rowconfigure(2, weight=1)

        self._build_io_panel(setup)
        self._build_sdr_panel(setup)
        self._build_status_panel(plot_area)
        self._build_plot_panel(plot_area)

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
        ttk.Button(frame, text="Refresh", command=self._refresh_gps_ports).grid(row=1, column=2, sticky="e")
        ttk.Label(frame, text="Baud").grid(row=2, column=0, sticky="w")
        gps_baud_entry = ttk.Entry(frame, textvariable=self.gps_baud_var, width=12)
        gps_baud_entry.grid(row=2, column=1, sticky="ew", padx=4)
        gps_baud_entry.bind("<Return>", lambda _event: self._commit_all_settings())

        self.csv_path_var = tk.StringVar(value=str(self._settings.get("csv_path", Path.home() / "radio_survey_logs" / "survey_log.csv")))
        ttk.Label(frame, text="CSV file").grid(row=3, column=0, sticky="w")
        csv_entry = ttk.Entry(frame, textvariable=self.csv_path_var, width=32)
        csv_entry.grid(row=3, column=1, sticky="ew", padx=4)
        csv_entry.bind("<Return>", lambda _event: self._commit_all_settings())
        ttk.Button(frame, text="Browse", command=self._browse_csv).grid(row=3, column=2, sticky="e")

        self.window_minutes_var = tk.IntVar(value=int(self._settings.get("plot_window_minutes", 10)))
        ttk.Label(frame, text="Plot window").grid(row=4, column=0, sticky="w")
        ttk.Scale(frame, from_=1, to=60, variable=self.window_minutes_var, command=lambda _v: self._commit_plot_window()).grid(row=4, column=1, sticky="ew", padx=4)
        window_spinbox = ttk.Spinbox(frame, from_=1, to=60, textvariable=self.window_minutes_var, width=5, command=self._commit_plot_window)
        window_spinbox.grid(row=4, column=2, sticky="e")
        window_spinbox.bind("<Return>", lambda _event: self._commit_plot_window())

        self.start_button = ttk.Button(frame, text="Start", command=self._start)
        self.start_button.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.stop_button = ttk.Button(frame, text="Stop", command=self._stop, state="disabled")
        self.stop_button.grid(row=5, column=1, sticky="ew", padx=4, pady=(8, 0))

        self.logging_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Log to CSV", variable=self.logging_enabled_var, command=self._toggle_logging).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.autoscale_y_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Autoscale Y", variable=self.autoscale_y_var, command=self._redraw_plot).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

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

    def _build_sdr_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="SDR Parameters", padding=8)
        frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        for row, param in enumerate(SDR_PARAMETER_DEFS):
            ttk.Label(frame, text=param.label).grid(row=row, column=0, sticky="w", pady=1)
            var = self._make_var(param)
            self._vars[param.key] = var
            widget = self._make_widget(frame, param, var)
            self._widgets[param.key] = widget
            widget.grid(row=row, column=1, sticky="ew", padx=4, pady=1)
            self._bind_commit(widget, param)
            self._last_valid_sdr_values[param.key] = self._coerce_param_value(param, var.get())
            if param.units:
                ttk.Label(frame, text=param.units).grid(row=row, column=2, sticky="w")

    def _build_status_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Realtime Data", padding=10)
        frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        for index in range(4):
            frame.columnconfigure(index, weight=1)

        self.status_var = tk.StringVar(value="Idle")
        self.position_var = tk.StringVar(value="No fix")
        self.timestamp_var = tk.StringVar(value="-")
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

    def _build_plot_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Spectrum (dBm)").grid(row=1, column=0, sticky="w", pady=(10, 2))
        ttk.Label(parent, text="Received Level (dBm)").grid(row=1, column=1, sticky="w", pady=(10, 2), padx=(10, 0))
        self.spectrum_canvas = tk.Canvas(parent, background="#101418", highlightthickness=0)
        self.spectrum_canvas.grid(row=2, column=0, sticky="nsew")
        self.spectrum_canvas.bind("<Configure>", lambda _event: self._redraw_spectrum())
        self.canvas = tk.Canvas(parent, background="#101418", highlightthickness=0)
        self.canvas.grid(row=2, column=1, sticky="nsew", padx=(10, 0))
        self.canvas.bind("<Configure>", lambda _event: self._redraw_plot())

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
        elif param.kind == "bool":
            widget.configure(command=self._commit_all_settings)
        else:
            widget.bind("<Return>", lambda _event: self._commit_all_settings())

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
            if self.logging_enabled_var.get():
                self._open_logger()
            self._gps_source = SimulatedGpsSource() if self.gps_sim_var.get() else SerialGpsSource(self.gps_port_var.get(), int(self.gps_baud_var.get()))
            self._gps_source.start(self._queue_fix, self._queue_error)
        except Exception as exc:
            self._cleanup()
            messagebox.showerror("Unable to start survey", str(exc))
            return

        self._running = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Running")

    def _stop(self) -> None:
        self._cleanup()
        self._running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("Stopped")

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
        except Exception as exc:
            self.status_var.set(f"Settings not applied: {exc}")

    def _commit_plot_window(self) -> None:
        self._redraw_plot()
        self._settings["plot_window_minutes"] = int(self.window_minutes_var.get())
        save_settings(self._settings)

    def _commit_y_axis(self, redraw: bool = True) -> None:
        if not self._apply_y_axis_fields():
            self.status_var.set("Y axis values must be between -120 and -10 dBm")
            return
        self._settings["plot_y_max_dbm"] = self._last_valid_y_max
        self._settings["plot_y_min_dbm"] = self._last_valid_y_min
        save_settings(self._settings)
        if redraw:
            self._redraw_plot()

    def _commit_spectrum_y_axis(self, redraw: bool = True) -> None:
        if not self._apply_spectrum_y_axis_fields():
            self.status_var.set("Spectrum Y axis values must be between -120 and -10 dBm")
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
        current = self._parse_y_value(var.get())
        if current is None:
            current = self._last_valid_spectrum_y_max if var is self.spectrum_y_max_var else self._last_valid_spectrum_y_min
        var.set(f"{self._clamp_y_value(current + delta_db):.0f}")
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
        y_max = self._parse_y_value(self.spectrum_y_max_var.get())
        y_min = self._parse_y_value(self.spectrum_y_min_var.get())
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

    def _clamp_y_value(self, value: float) -> float:
        return min(-10.0, max(-120.0, value))

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
            "plot_window_minutes": int(self.window_minutes_var.get()),
            "plot_y_max_dbm": self._last_valid_y_max,
            "plot_y_min_dbm": self._last_valid_y_min,
            "spectrum_y_max_dbm": self._last_valid_spectrum_y_max,
            "spectrum_y_min_dbm": self._last_valid_spectrum_y_min,
            "spectrum_averages": self._last_valid_spectrum_averages,
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
        backend = str(params["backend"])
        if self._level_meter is not None and self._active_sdr_backend == backend:
            self._level_meter.update_settings(params)
            return

        if self._level_meter is not None:
            self._level_meter.close()
        new_meter = create_level_meter(backend)
        new_meter.configure(params)
        self._level_meter = new_meter
        self._active_sdr_backend = backend

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
                self.status_var.set(str(payload))
        self.after(100, self._process_events)

    def _handle_fix(self, fix: GpsFix) -> None:
        if not isinstance(fix, GpsFix) or self._level_meter is None:
            return
        try:
            level = self._level_meter.read_level_dbm()
            if self.logging_enabled_var.get() and self._logger is not None:
                self._logger.write(fix, level)
        except Exception as exc:
            self.status_var.set(str(exc))
            return

        self.position_var.set(fix.position_dms)
        self.timestamp_var.set(fix.timestamp_utc.strftime("%H:%M:%S UTC"))
        self.level_var.set(f"{level:.2f} dBm")
        self._points.append(LevelPoint(time.time(), level))
        self._update_spectrum_average()
        self._redraw_spectrum()
        self._redraw_plot()

    def _redraw_plot(self) -> None:
        canvas = self.canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")

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
            canvas.create_line(margin_left, y, width - margin_right, y, fill="#202a33")

        if not self._points:
            canvas.create_text(width / 2, height / 2, text="Waiting for GPS fixes", fill="#b7c0c9", font=("TkDefaultFont", 13))
            return

        now = time.time()
        window_s = int(self.window_minutes_var.get()) * 60
        visible = [point for point in self._points if point.epoch_s >= now - window_s]
        if not visible:
            return

        if self.autoscale_y_var.get():
            min_level = min(point.level_dbm for point in visible)
            max_level = max(point.level_dbm for point in visible)
            span = max(max_level - min_level, 10.0)
            low = min_level - (span * 0.1)
            high = max_level + (span * 0.1)
        else:
            self._apply_y_axis_fields()
            low = self._last_valid_y_min
            high = self._last_valid_y_max

        for label_value in (low, (low + high) / 2, high):
            y = margin_top + (high - label_value) / (high - low) * plot_h
            canvas.create_text(margin_left - 8, y, text=f"{label_value:.0f}", fill="#b7c0c9", anchor="e", font=("TkDefaultFont", 9))

        coords: list[float] = []
        for point in visible:
            x = margin_left + (point.epoch_s - (now - window_s)) / window_s * plot_w
            y = margin_top + (high - point.level_dbm) / (high - low) * plot_h
            coords.extend((x, y))

        if len(coords) >= 4:
            canvas.create_line(*coords, fill="#5cc8ff", width=2, smooth=True)
        x, y = coords[-2], coords[-1]
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#ffffff", outline="#5cc8ff")
        canvas.create_text(margin_left, height - 16, text=f"-{self.window_minutes_var.get()} min", fill="#b7c0c9", anchor="w")
        canvas.create_text(width - margin_right, height - 16, text="now", fill="#b7c0c9", anchor="e")

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
        powers = tuple(float(value) for value in spectrum.powers_dbm)
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


def main() -> None:
    app = SurveyApp()
    app.mainloop()
