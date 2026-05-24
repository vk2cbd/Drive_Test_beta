from __future__ import annotations

import queue
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
        self.geometry("1180x760")
        self.minsize(980, 640)

        self._gps_source = None
        self._level_meter: LevelMeter | None = None
        self._logger: CsvSurveyLogger | None = None
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._points: list[LevelPoint] = []
        self._vars: dict[str, tk.Variable] = {}
        self._widgets: dict[str, ttk.Widget] = {}
        self._settings = load_settings()
        self._running = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        setup = ttk.Frame(self, padding=10)
        setup.grid(row=0, column=0, sticky="nsw")
        setup.columnconfigure(1, weight=1)

        plot_area = ttk.Frame(self, padding=(0, 10, 10, 10))
        plot_area.grid(row=0, column=1, sticky="nsew")
        plot_area.columnconfigure(0, weight=1)
        plot_area.rowconfigure(2, weight=1)

        self._build_io_panel(setup)
        self._build_sdr_panel(setup)
        self._build_status_panel(plot_area)
        self._build_plot_panel(plot_area)

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
            if param.units:
                ttk.Label(frame, text=param.units).grid(row=row, column=2, sticky="w")

    def _build_status_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Realtime Data", padding=10)
        frame.grid(row=0, column=0, sticky="ew")
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
        ttk.Label(parent, text="Received Level (dBm)").grid(row=1, column=0, sticky="w", pady=(10, 2))
        self.canvas = tk.Canvas(parent, background="#101418", highlightthickness=0)
        self.canvas.grid(row=2, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self._redraw_plot())

    def _make_var(self, param: ParameterDef) -> tk.Variable:
        value = self._settings.get(param.key, param.default)
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
        self._logger = None

    def _commit_all_settings(self) -> None:
        try:
            self._format_frequency_field()
            self._save_current_settings()
            if self._running:
                self._restart_gps_source()
                self._reconfigure_level_meter()
                self._sync_logger_state()
                self.status_var.set("Settings applied")
        except Exception as exc:
            self.status_var.set(f"Settings not applied: {exc}")

    def _commit_plot_window(self) -> None:
        self._redraw_plot()
        self._settings["plot_window_minutes"] = int(self.window_minutes_var.get())
        save_settings(self._settings)

    def _save_current_settings(self) -> None:
        settings = {
            "gps_simulated": self.gps_sim_var.get(),
            "gps_port": self.gps_port_var.get(),
            "gps_baud": int(self.gps_baud_var.get()),
            "csv_path": self.csv_path_var.get(),
            "plot_window_minutes": int(self.window_minutes_var.get()),
        }
        settings.update(self._collect_sdr_display_params())
        self._settings = settings
        save_settings(settings)

    def _collect_sdr_display_params(self) -> dict[str, object]:
        params: dict[str, object] = {}
        definitions = {param.key: param for param in SDR_PARAMETER_DEFS}
        for key, var in self._vars.items():
            param = definitions[key]
            value = var.get()
            if param.kind == "bool":
                params[key] = bool(value)
            elif param.kind == "int":
                params[key] = int(value)
            elif param.kind == "float":
                params[key] = float(value)
            elif param.kind == "choice":
                params[key] = self._coerce_choice(value)
            else:
                params[key] = str(value)
        return params

    def _restart_gps_source(self) -> None:
        if self._gps_source is not None:
            self._gps_source.stop()
        self._gps_source = SimulatedGpsSource() if self.gps_sim_var.get() else SerialGpsSource(self.gps_port_var.get(), int(self.gps_baud_var.get()))
        self._gps_source.start(self._queue_fix, self._queue_error)

    def _reconfigure_level_meter(self) -> None:
        if self._level_meter is not None:
            self._level_meter.close()
        params = self._collect_sdr_params()
        self._level_meter = create_level_meter(str(params["backend"]))
        self._level_meter.configure(params)

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
            value = float(self._vars["center_frequency_mhz"].get())
            self._vars["center_frequency_mhz"].set(f"{value:.6f}")

    def _collect_sdr_params(self) -> dict[str, object]:
        params: dict[str, object] = {}
        definitions = {param.key: param for param in SDR_PARAMETER_DEFS}
        for key, var in self._vars.items():
            param = definitions[key]
            value = var.get()
            if param.kind == "choice":
                value = self._coerce_choice(value)
            elif param.kind == "int":
                value = int(value)
            elif param.kind == "float":
                value = float(value)
            params[key] = value
        if "center_frequency_mhz" in params:
            params["center_frequency_hz"] = float(params.pop("center_frequency_mhz")) * 1_000_000.0
        return params

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
        self._trim_points()
        self._redraw_plot()

    def _trim_points(self) -> None:
        cutoff = time.time() - int(self.window_minutes_var.get()) * 60
        self._points = [point for point in self._points if point.epoch_s >= cutoff]

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

        min_level = min(point.level_dbm for point in visible)
        max_level = max(point.level_dbm for point in visible)
        span = max(max_level - min_level, 10.0)
        low = min_level - (span * 0.1)
        high = max_level + (span * 0.1)

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

    def _on_close(self) -> None:
        self._cleanup()
        self.destroy()


def main() -> None:
    app = SurveyApp()
    app.mainloop()
