# Radio Network Survey Logger

Version: `0.2.0-alpha`

Python GUI application for surveying a radio network with:

- USB GPS receiver emitting NMEA sentences
- SDRplay RSPdx SDR, currently through an optional SoapySDR backend
- Real-time geographic position display
- Received-level logging to CSV
- Scrolling received-level plot with a configurable 1 to 60 minute time window
- Live spectrum display using the configured center frequency and IF bandwidth

The app includes simulator modes for both GPS and SDR so the GUI and logging flow can be tested before hardware is connected.

## Ubuntu Quick Start

Install the Ubuntu Python GUI and virtual-environment packages:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-tk
```

Create a virtual environment and start the app:

```bash
cd ~/Drive_Test
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m radio_survey
```

If you do not have `pyserial` or `SoapySDR` available yet, the app can still run with simulated GPS and received level data.

## GUI Settings

Most GUI fields are remembered between app runs in:

```text
~/.config/radio_survey/settings.json
```

Text and numeric fields are committed when you press Enter in the field. When the survey is running, committed GPS and SDR changes are applied by restarting the affected device path where possible. The center frequency field is entered in MHz with six decimal places.

Sample rate is entered in Msps, and IF bandwidth is entered in MHz. The app converts those values to Hz before configuring the SDR backend.

SDR numeric fields with configured ranges reject invalid or out-of-range values and keep the last valid setting. Current guarded ranges include RF gain reduction 0 to 66 dB, IF gain reduction 20 to 59 dB, and LNA state 0 to 9.

CSV logging is controlled by the **Log to CSV** button. It always defaults to off when the app starts and is not remembered between sessions.

The received-level plot defaults to a manual Y axis. **Y max** and **Y min** are remembered between runs, can be typed directly, and have +/- buttons that adjust the value by 5 dB per press. Manual Y-axis values must be between -120 dBm and -10 dBm; values outside that range are ignored. **Autoscale Y** always defaults to off when the app starts.

The spectrum display sits to the left of the received-level plot. Its frequency axis follows the configured center frequency and IF bandwidth. Spectrum Y-axis controls mirror the power plot controls and also default to manual scaling. **Spec averages** applies display averaging to the spectrum trace and accepts integer values from 1 to 100.

Changing the plot time window only changes which samples are visible. It does not delete the in-memory display history for the current app session.

## Ubuntu GPS Setup

USB GPS receivers normally appear as one of these devices:

```text
/dev/ttyUSB0
/dev/ttyACM0
/dev/serial/by-id/...
```

The GUI defaults to `/dev/ttyUSB0` at 4800 baud and has a **Refresh** button that scans those Linux serial-device paths. If your user cannot open the GPS device, add yourself to the `dialout` group and then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

You can check which port was created after plugging in the GPS with:

```bash
dmesg | tail
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

If the app reports that the device is ready but returned no data, another Linux service may already have the GPS serial port open. Check for that with:

```bash
sudo lsof /dev/ttyUSB0
sudo systemctl status gpsd ModemManager
```

Replace `/dev/ttyUSB0` with the port shown in the app. If either service is using the GPS, stop it before running the survey:

```bash
sudo systemctl stop gpsd
sudo systemctl stop ModemManager
```

For a direct NMEA test outside the app:

```bash
source .venv/bin/activate
python3 -m serial.tools.miniterm /dev/ttyUSB0 4800
```

## CSV Format

The logger writes:

```csv
date_local,time_local,latitude,longitude,received_level_dbm
```

GPS time is converted from UTC to the computer's local timezone when the row is written. Latitude and longitude are formatted separately in degrees, minutes, and seconds.

## Ubuntu SDRplay Setup

For an SDRplay RSPdx, install the SDRplay API/runtime and a SoapySDR SDRplay module if you want to use the included `soapy_sdrplay` backend. The application keeps SDR configuration in the GUI and passes supported settings into the backend where possible.

The app talks to the RSPdx through SoapySDR and the SoapySDR SDRplay API 3 plugin. The RSPdx has three software-selectable antenna ports; the GUI exposes antenna choices A, B, and C.

If the app reports `SoapySDR::Device::make() no match`, SoapySDR cannot find a device matching the GUI's **Device args** field. First check whether SoapySDR can see the RSPdx:

```bash
SoapySDRUtil --find
SoapySDRUtil --probe="driver=sdrplay"
```

If `SoapySDRUtil` is missing, install the SoapySDR tools package:

```bash
sudo apt install soapysdr-tools
```

If `--find` does not list an SDRplay device, install or repair the SDRplay API/runtime and the SoapySDR SDRplay module for your Ubuntu version. When `--find` succeeds, copy the reported driver/device arguments into the GUI **Device args** field. The default is:

```text
driver=sdrplay
```

SDRconnect introduced a WebSocket/module system after v1.0.5. A future backend can be added under `radio_survey/sdr.py` if you want this app to drive SDRconnect directly rather than sample the SDR through SoapySDR.

## Project Layout

- `radio_survey/app.py` - Tkinter GUI, plot, control loop, and logging workflow
- `radio_survey/config.py` - SDR parameter definitions used to build the GUI
- `radio_survey/gps.py` - serial and simulated GPS sources
- `radio_survey/nmea.py` - NMEA parsing and coordinate formatting
- `radio_survey/sdr.py` - simulated and SoapySDR received-level/spectrum backends
- `radio_survey/logger.py` - CSV writer
- `tests/` - focused tests for parsing and logging
