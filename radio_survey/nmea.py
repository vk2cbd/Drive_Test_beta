from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone


@dataclass(frozen=True)
class GpsFix:
    timestamp_utc: datetime
    latitude_deg: float
    longitude_deg: float
    altitude_m: float | None = None
    quality: int | None = None
    satellites: int | None = None
    bearing_deg: float | None = None

    @property
    def position_dms(self) -> str:
        return f"{decimal_to_dms(self.latitude_deg, True)} {decimal_to_dms(self.longitude_deg, False)}"


def parse_nmea(sentence: str, current_date: date | None = None) -> GpsFix | None:
    sentence = sentence.strip()
    if not sentence.startswith("$"):
        return None

    without_checksum = sentence[1:].split("*", 1)[0]
    parts = without_checksum.split(",")
    talker_type = parts[0][-3:]

    if talker_type == "GGA":
        return _parse_gga(parts, current_date or datetime.now(timezone.utc).date())
    if talker_type == "RMC":
        return _parse_rmc(parts, current_date)
    return None


def _parse_gga(parts: list[str], fix_date: date) -> GpsFix | None:
    if len(parts) < 10 or not parts[1] or not parts[2] or not parts[4]:
        return None
    quality = _safe_int(parts[6])
    if quality == 0:
        return None

    fix_time = _parse_time(parts[1])
    if fix_time is None:
        return None

    return GpsFix(
        timestamp_utc=datetime.combine(fix_date, fix_time, tzinfo=timezone.utc),
        latitude_deg=_parse_lat_lon(parts[2], parts[3]),
        longitude_deg=_parse_lat_lon(parts[4], parts[5]),
        altitude_m=_safe_float(parts[9]),
        quality=quality,
        satellites=_safe_int(parts[7]),
    )


def _parse_rmc(parts: list[str], current_date: date | None) -> GpsFix | None:
    if len(parts) < 10 or parts[2] != "A" or not parts[1] or not parts[3] or not parts[5]:
        return None

    fix_time = _parse_time(parts[1])
    fix_date = _parse_date(parts[9]) or current_date
    if fix_time is None or fix_date is None:
        return None

    return GpsFix(
        timestamp_utc=datetime.combine(fix_date, fix_time, tzinfo=timezone.utc),
        latitude_deg=_parse_lat_lon(parts[3], parts[4]),
        longitude_deg=_parse_lat_lon(parts[5], parts[6]),
        bearing_deg=_safe_float(parts[8]),
    )


def _parse_time(value: str) -> time | None:
    try:
        hour = int(value[0:2])
        minute = int(value[2:4])
        seconds = float(value[4:])
        second = int(seconds)
        microsecond = int((seconds - second) * 1_000_000)
        return time(hour, minute, second, microsecond)
    except (ValueError, IndexError):
        return None


def _parse_date(value: str) -> date | None:
    try:
        day = int(value[0:2])
        month = int(value[2:4])
        year = 2000 + int(value[4:6])
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


def _parse_lat_lon(value: str, hemisphere: str) -> float:
    dot = value.find(".")
    degree_digits = dot - 2 if dot >= 0 else len(value) - 2
    degrees = int(value[:degree_digits])
    minutes = float(value[degree_digits:])
    decimal = degrees + minutes / 60.0
    if hemisphere in ("S", "W"):
        decimal *= -1.0
    return decimal


def decimal_to_dms(value: float, latitude: bool) -> str:
    hemisphere = ("N" if value >= 0 else "S") if latitude else ("E" if value >= 0 else "W")
    absolute = abs(value)
    degrees = int(absolute)
    minutes_float = (absolute - degrees) * 60.0
    minutes = int(minutes_float)
    seconds = int(round((minutes_float - minutes) * 60.0))
    if seconds >= 60:
        seconds = 0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        degrees += 1
    return f"{degrees:02d}:{minutes:02d}:{seconds:02d}{hemisphere}"


def _safe_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
