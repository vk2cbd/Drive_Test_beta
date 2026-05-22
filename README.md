# Radio Network Survey Logger

Python GUI application for surveying a radio network with:

- USB GPS receiver emitting NMEA sentences
- SDRplay RSPdx SDR, currently through an optional SoapySDR backend
- Real-time geographic position display
- Received-level logging to CSV
- Scrolling received-level plot with a configurable 1 to 60 minute time window

The app includes simulator modes for both GPS and SDR so the GUI and logging flow can be tested before hardware is connected.

## Quick Start

Install Python 3.11 or newer first if `python` is not already available in PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m radio_survey
```

If you do not have `pyserial` or `SoapySDR` available yet, the app can still run with simulated GPS and received level data.

## CSV Format

The logger writes:

```csv
timestamp_utc,gps_position,received_level_dbm
```

`gps_position` is formatted as latitude and longitude in degrees, minutes, and seconds.

## Hardware Notes

For an SDRplay RSPdx, install the SDRplay API/runtime and a SoapySDR SDRplay module if you want to use the included `soapy_sdrplay` backend. The application keeps SDR configuration in the GUI and passes supported settings into the backend where possible.

SDRconnect introduced a WebSocket/module system after v1.0.5. A future backend can be added under `radio_survey/sdr.py` if you want this app to drive SDRconnect directly rather than sample the SDR through SoapySDR.

## Project Layout

- `radio_survey/app.py` - Tkinter GUI, plot, control loop, and logging workflow
- `radio_survey/config.py` - SDR parameter definitions used to build the GUI
- `radio_survey/gps.py` - serial and simulated GPS sources
- `radio_survey/nmea.py` - NMEA parsing and coordinate formatting
- `radio_survey/sdr.py` - simulated and SoapySDR received-level backends
- `radio_survey/logger.py` - CSV writer
- `tests/` - focused tests for parsing and logging
