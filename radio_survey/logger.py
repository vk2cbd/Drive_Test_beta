from __future__ import annotations

import csv
from pathlib import Path
from typing import TextIO

from .nmea import GpsFix


class CsvSurveyLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: TextIO | None = None
        self._writer: csv.writer | None = None

    def open(self) -> None:
        exists = self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if not exists:
            self._writer.writerow(("timestamp_utc", "gps_position", "received_level_dbm"))

    def write(self, fix: GpsFix, level_dbm: float) -> None:
        if self._writer is None:
            raise RuntimeError("CSV logger is not open")
        self._writer.writerow((fix.timestamp_utc.isoformat(), fix.position_dms, f"{level_dbm:.2f}"))
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None

    def __enter__(self) -> "CsvSurveyLogger":
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

