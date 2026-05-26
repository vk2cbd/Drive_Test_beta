# Radio Network Survey Logger

Version: `0.4.6-beta`

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

The spectrum display sits to the left of the received-level plot. Its frequency axis follows the configured center frequency and IF bandwidth. Spectrum Y-axis controls mirror the power plot controls and also default to manual scaling, with manual values from -150 dBm to -10 dBm. **Spec averages** applies display averaging to the spectrum trace and accepts integer values from 1 to 100.

The received-level plot now uses the **Power meas BW** field, in kHz, to measure channel power around the configured center frequency instead of always using the full SDR bandwidth. The effective measurement bandwidth is capped by the configured sample rate and IF bandwidth. This usually makes a narrow signal-generator carrier much easier to see. The value is still relative until calibrated with **dBm calibration offset**.

If the GPS emits more than one valid position sentence for the same GPS second, such as both RMC and GGA, the app updates the realtime GPS metadata but only records one SDR level sample, plot point, and CSV row for that second.

Changing the plot time window or SDR parameters only changes newly plotted samples. It does not delete the in-memory display history for the current app session, so parameter changes can be compared on the same trace. The plot window control sits below the received-level plot. The received-level plot advances only when GPS fixes add new samples, draws vertical time graticles at 25%, 50%, and 75% of the visible time span, can be zoomed by dragging a visible rectangle with the mouse, and the **Back** button returns to the previous plot view. Returning from a drag zoom to the normal scrolling view allows new GPS samples to scroll the plot again.

## VHF Calibration

The beta calibration workflow stores one VHF broadcast calibration profile at:

```text
~/.config/radio_survey/calibration_vhf_100mhz.json
```

Set the GUI to the exact SDR settings you want to calibrate, start the survey, then click **New VHF 100 MHz cal**. This records the current SDR GUI settings as calibration metadata. Select each calibration point and click **Capture point** while the Agilent generator is set accordingly:

- Noise floor, with no signal input
- `-100 dBm`
- `-80 dBm`
- `-60 dBm`
- `-40 dBm`
- `1 dB compression`, with the generator level entered in **Compression dBm**

The app interpolates or extrapolates between the signal-level calibration points and applies the correction to the received level, CSV log, and spectrum display. For this VHF broadcast profile, centre frequency remains valid anywhere from 88 to 108 MHz. Tuner, antenna, HDR mode, Bias T, DAB notch, FM notch, and MW notch changes do not invalidate the calibration status. Other current SDR GUI settings must match the stored calibration metadata, and centre frequency must stay in the VHF broadcast range, otherwise the calibration status text turns red and the calibration is not applied until the settings match again.

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

GPS time is converted from UTC to the computer's local timezone when the row is written. The GUI also displays GPS time in local time. Latitude and longitude are formatted separately in degrees, minutes, and seconds.

## Ubuntu SDRplay Setup

For an SDRplay RSPdx, install the SDRplay API/runtime and a SoapySDR SDRplay module if you want to use the included `soapy_sdrplay` backend. The application keeps SDR configuration in the GUI and passes supported settings into the backend where possible.

The app talks to the RSPdx through SoapySDR and the SoapySDR SDRplay API 3 plugin. The RSPdx has three software-selectable antenna ports; the GUI exposes antenna choices A, B, and C. When the survey starts, the app shows the SDR settings that were actually applied and warnings for optional settings that the installed SoapySDRplay module does not expose.

The SDR stream runs continuously while the survey is active. GPS fixes snapshot the most recent SDR level rather than starting a fresh SDR read each time.

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
