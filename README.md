# Radio Network Survey Logger

Python GUI application for surveying a radio network with:

- USB GPS receiver emitting NMEA sentences
- SDRplay RSPdx SDR, currently through an optional SoapySDR backend
- Real-time geographic position display
- Received-level logging to CSV
- Scrolling received-level plot with a configurable 1 to 60 minute time window

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

## Ubuntu GPS Setup

USB GPS receivers normally appear as one of these devices:

```text
/dev/ttyUSB0
/dev/ttyACM0
/dev/serial/by-id/...
```

The GUI defaults to `/dev/ttyUSB0` and has a **Refresh** button that scans those Linux serial-device paths. If your user cannot open the GPS device, add yourself to the `dialout` group and then log out and back in:

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
python3 -m serial.tools.miniterm /dev/ttyUSB0 9600
```

## CSV Format

The logger writes:

```csv
timestamp_utc,gps_position,received_level_dbm
```

`gps_position` is formatted as latitude and longitude in degrees, minutes, and seconds.

## Ubuntu SDRplay Setup

For an SDRplay RSPdx, install the SDRplay API/runtime and a SoapySDR SDRplay module if you want to use the included `soapy_sdrplay` backend. The application keeps SDR configuration in the GUI and passes supported settings into the backend where possible.

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
- `radio_survey/sdr.py` - simulated and SoapySDR received-level backends
- `radio_survey/logger.py` - CSV writer
- `tests/` - focused tests for parsing and logging
