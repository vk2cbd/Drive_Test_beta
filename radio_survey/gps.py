from __future__ import annotations

import glob
import math
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Protocol

from .nmea import GpsFix, parse_nmea


FixCallback = Callable[[GpsFix], None]
ErrorCallback = Callable[[str], None]

LINUX_GPS_PORT_PATTERNS = (
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
    "/dev/serial/by-id/*",
)


def discover_gps_ports() -> list[str]:
    ports: list[str] = []
    for pattern in LINUX_GPS_PORT_PATTERNS:
        ports.extend(glob.glob(pattern))
    return sorted(dict.fromkeys(ports))


def default_gps_port() -> str:
    ports = discover_gps_ports()
    return ports[0] if ports else "/dev/ttyUSB0"


class GpsSource(Protocol):
    def start(self, on_fix: FixCallback, on_error: ErrorCallback) -> None: ...
    def stop(self) -> None: ...


class SerialGpsSource:
    def __init__(self, port: str, baud: int = 4800) -> None:
        self.port = port
        self.baud = baud
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_fix: FixCallback, on_error: ErrorCallback) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(on_fix, on_error), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self, on_fix: FixCallback, on_error: ErrorCallback) -> None:
        try:
            import serial
        except ImportError:
            on_error("pyserial is not installed. Install requirements or use simulated GPS.")
            return

        while not self._stop_event.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=1.0) as ser:
                    self._read_loop(ser, on_fix)
            except serial.SerialException as exc:
                on_error(
                    "GPS serial read failed. Check that the GPS is still plugged in and that "
                    f"gpsd or ModemManager is not also using {self.port}: {exc}"
                )
                self._stop_event.wait(2.0)
            except Exception as exc:
                on_error(f"GPS error: {exc}. On Ubuntu, check the port path and dialout group permissions.")
                self._stop_event.wait(2.0)

    def _read_loop(self, ser: object, on_fix: FixCallback) -> None:
        while not self._stop_event.is_set():
            raw_bytes = ser.readline()
            if not raw_bytes:
                continue
            for raw in raw_bytes.decode("ascii", errors="ignore").splitlines():
                raw = raw.strip()
                if raw:
                    fix = parse_nmea(raw)
                    if fix is not None:
                        on_fix(fix)


class SimulatedGpsSource:
    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_fix: FixCallback, on_error: ErrorCallback) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(on_fix,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self, on_fix: FixCallback) -> None:
        start = time.monotonic()
        base_lat = -33.8688
        base_lon = 151.2093
        while not self._stop_event.is_set():
            elapsed = time.monotonic() - start
            on_fix(
                GpsFix(
                    timestamp_utc=datetime.now(timezone.utc),
                    latitude_deg=base_lat + math.sin(elapsed / 30.0) * 0.001,
                    longitude_deg=base_lon + math.cos(elapsed / 30.0) * 0.001,
                    altitude_m=24.0,
                    quality=1,
                    satellites=10,
                )
            )
            time.sleep(self.interval_s)
