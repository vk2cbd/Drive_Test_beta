from datetime import date

from radio_survey.nmea import decimal_to_dms, parse_nmea


def test_parse_gga_sentence() -> None:
    fix = parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47", date(2026, 5, 22))

    assert fix is not None
    assert fix.timestamp_utc.isoformat() == "2026-05-22T12:35:19+00:00"
    assert round(fix.latitude_deg, 6) == 48.117300
    assert round(fix.longitude_deg, 6) == 11.516667
    assert fix.altitude_m == 545.4
    assert fix.satellites == 8


def test_parse_rmc_sentence_with_date() -> None:
    fix = parse_nmea("$GPRMC,092751.000,A,5321.6802,N,00630.3372,W,0.06,31.66,280511,,,A*43")

    assert fix is not None
    assert fix.timestamp_utc.isoformat() == "2011-05-28T09:27:51+00:00"
    assert round(fix.latitude_deg, 6) == 53.361337
    assert round(fix.longitude_deg, 6) == -6.505620
    assert fix.bearing_deg == 31.66


def test_parse_rmc_replaces_implausible_date_when_current_date_supplied() -> None:
    fix = parse_nmea("$GPRMC,092751.000,A,5321.6802,N,00630.3372,W,0.06,31.66,101006,,,A*43", date(2026, 5, 26))

    assert fix is not None
    assert fix.timestamp_utc.isoformat() == "2026-05-26T09:27:51+00:00"


def test_decimal_to_dms() -> None:
    assert decimal_to_dms(-33.8688, latitude=True).endswith("S")
    assert decimal_to_dms(151.2093, latitude=False).endswith("E")
    assert decimal_to_dms(-33.8688, latitude=True) == "33:52:08S"
    assert decimal_to_dms(151.2093, latitude=False) == "151:12:33E"
