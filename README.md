# Cantilever Beam DAQ

Desktop application for the cantilever bending experiment on a **Raspberry Pi 5** with the
**Strain-PZT DAQ HAT rev 1.3** (ADS1256 + INA333 + OPA197 + REF5025 + DS18B20).

- **Live view**: strain (µε), tip deflection (mm), equivalent tip load (g), PZT voltage and charge, temperature
- **Tare**: zeroes strain and PZT and stores the reference temperature
- **Recording**: saves to CSV (or Excel), plus a JSON side-car file holding every setting
- **Calibration**: known masses on the tip → fit against beam theory → applied to all values
- **Temperature compensation**: measures the thermal drift of the semiconductor gauge with the DS18B20 and removes it
- **Analysis**: statistics (pandas), time series and distribution (seaborn), natural frequency from the
  PZT spectrum compared with Euler-Bernoulli theory, thermal-drift fit
- **Simulation mode**: a physics-based simulator of the whole HAT, so you can learn and test the program on any PC

---

## 1. Install on the Raspberry Pi 5 (Raspberry Pi OS Bookworm or newer)

All libraries come from the Raspberry Pi OS package manager. No `pip` and no virtual environment are needed.

```bash
sudo apt update
sudo apt install python3-tk python3-numpy python3-pandas python3-matplotlib \
                 python3-seaborn python3-spidev python3-lgpio python3-openpyxl
```

Enable SPI and 1-Wire:

```bash
sudo raspi-config nonint do_spi 0
echo "dtoverlay=w1-gpio,gpiopin=26" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

After the reboot, check the hardware:

```bash
ls /dev/spidev0.0                 # SPI is on
ls /sys/bus/w1/devices/           # should list a 28-xxxxxxxxxxxx folder (the DS18B20)
```

Your user must be in the `spi` and `gpio` groups (the default user already is):
`sudo usermod -aG spi,gpio $USER`, then log out and back in.

## 2. Run

**The file to run is `run_app.py`** in the top folder. It checks that all libraries are installed,
then starts the program. The other `.py` files are modules that it loads.

**Windows PC (simulation only, no hardware):** install Python 3.9+ from python.org (keep
"tcl/tk and IDLE" ticked), install the libraries once, then double-click **`run_windows.bat`**, or:

```powershell
cd C:\Users\<you>\Downloads\cantilever_daq_project\cantilever_daq_project
python -m pip install numpy pandas matplotlib seaborn openpyxl
python run_app.py
```

**Raspberry Pi:** copy the folder `cantilever_daq_project` to the Pi (for example to `/home/pi/`), then:

```bash
cd ~/cantilever_daq_project
python3 run_app.py              # uses the hardware if spidev + lgpio are installed
python3 run_app.py --simulate   # simulation mode
```

or double-click `run.sh` / `run_simulation.sh`. To get a desktop icon, copy `cantilever-daq.desktop`
to `~/Desktop/` and edit the path inside if your folder is somewhere else.

Other ways that also work: `python -m cantilever_daq` from the top folder, or `python gui.py` inside the
`cantilever_daq` folder.

## 3. Running an experiment

1. **Settings tab**: enter your beam (free length from the clamp to the load point, gauge position measured
   from the clamp, width, thickness, E-modulus) and the **gauge factor printed on the KSPB gauge package**.
   Select the bridge mode that matches the jumpers (half bridge = JP2 closed). Press **Apply**.
   Settings are saved automatically in `~/.config/cantilever_daq/settings.json`.
2. **Start**, wait a few seconds, then press **Tare** with the beam **unloaded**.
3. **Calibration tab** (recommended, the semiconductor gauge is non-linear and its gauge factor is approximate):
   hang a known mass on the tip, type the mass, press **Capture point**. Repeat for 4–6 masses
   (for example 0, 200, 500, 1000, 2000 g). Press **Fit**, check R² and the residuals, then **Apply calibration**.
4. **Temperature compensation** (once per beam/gauge): record the *unloaded* beam for 10–30 minutes while the
   temperature changes (for example warm the room or use a hair dryer from a distance, carefully). Then
   **Analysis tab → Analyse current recording → Use fitted α for compensation**, and press **Tare**.
   The program warns if the fit is unreliable (R² < 0.8, usually because the beam was loaded).
5. **Measure**: press **Record**, apply the loads or tap the beam, press **Stop recording**, then **Save recording…**.
6. **Analysis tab**: statistics, measured natural frequency vs theory, thermal drift. **Save figure…** exports PNG/PDF/SVG.

## 4. Data file columns

| Column | Meaning |
|---|---|
| `t_s` | time since the start of the recording [s] |
| `v_strain_V`, `v_pzt_V` | raw ADC differential voltages AIN0−AIN1 and AIN2−AIN3 [V] |
| `temp_C` | DS18B20 temperature [°C] |
| `bridge_ratio` | bridge output / excitation |
| `strain_raw_ue` | strain after tare [µε] |
| `strain_tc_ue` | strain after temperature compensation [µε] |
| `strain_ue` | final strain after calibration [µε] |
| `deflection_mm`, `force_N`, `load_g` | tip deflection, tip force and equivalent tip mass from beam theory |
| `pzt_mV`, `pzt_charge_nC` | charge amplifier output after tare, and the PZT charge (Q = −V·Cf) |
| `strain_clip`, `pzt_clip` | 1 when an amplifier is at its limit |

## 5. Formulas used

- Half bridge: ε = −2·r / GF; quarter bridge: ε = −4·r / (GF·(1 + 2r)); full bridge: ε = −r / GF,
  with r = (AIN0 − AIN1) / (G_INA · V_exc). Set *Polarity* to −1 if tension reads negative.
- Strain at the gauge: ε = 6·F·(L − x) / (E·b·h²); tip deflection: δ = 2·L³·ε / (3·h·(L − x))
- First natural frequency: f₁ = (1.8751² / 2πL²)·√(E·I / ρ·A) (no tip mass)
- Temperature compensation: ε_tc = ε − α·(T − T_tare)

## 6. Troubleshooting

| Message | Fix |
|---|---|
| *Missing Python modules: spidev, lgpio* | `sudo apt install python3-spidev python3-lgpio`, or use simulation mode |
| *Cannot open /dev/spidev0.0* | enable SPI (`sudo raspi-config nonint do_spi 0`) and reboot |
| *ADS1256 not detected (STATUS ID = …)* | check SPI wiring, CS on GPIO22, 5 V and 3.3 V on the HAT |
| *DRDY timeout* | check the 7.68 MHz crystal and its 27 pF capacitors, and DRDY on GPIO17 |
| *Cannot claim GPIO pins* | another program uses GPIO4/17/22; close it, or add your user to the `gpio` group |
| *no DS18B20* in the status line | check `dtoverlay=w1-gpio,gpiopin=26`, the 4.7 kΩ pull-up and the wiring on J5 |
| *PZT charge amp saturated* | the PZT signal exceeds ±2.4 V; increase C26 (see hardware note) |
| *Tkinter is missing* | `sudo apt install python3-tk` |

The log file is `~/.cache/cantilever_daq/cantilever_daq.log`.

## 7. Hardware note: PZT feedback capacitor

The T109-A5NO-1803 plate produces several nC per µε when bonded to the beam. With Cf = C26 = 10 nF the charge
amplifier saturates at only a few µε of dynamic strain (the simulator shows this when you remove a large mass quickly).
For larger vibrations, fit **C26 = 100 nF C0G (1206, e.g. Murata GRM31C5C1H104JA01L)** and **R51 = 10 MΩ**
(keeps the low cut-off at 0.16 Hz), then set *Feedback capacitor Cf* = 100 nF in Settings.

## 8. Tests

```bash
python3 -m unittest discover -s . -p "test_*.py" -v
```

25 tests cover the bridge and beam formulas, the configuration, the DS18B20 reader, the ADS1256 driver
(against an emulated chip), the simulator, the full controller workflow, and a GUI smoke test that drives
every button.

## 9. Project structure

```
cantilever_daq/
  config.py       settings dataclasses, JSON load/save, validation
  processing.py   bridge and beam formulas, signal processing, spectrum, fits
  hardware.py     ADS1256 driver (spidev + lgpio), DS18B20 reader, simulator
  acquisition.py  acquisition and temperature threads, ring buffer, experiment controller
  analysis.py     pandas statistics, seaborn/matplotlib plots
  gui.py          Tkinter user interface
  __main__.py     command-line entry point
tests/            unit tests and GUI smoke test
```
