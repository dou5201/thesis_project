"""Hardware access: ADS1256 over SPI (spidev + lgpio), DS18B20 over 1-Wire, and a simulator.

Every front end implements the same small interface:
    open(), close(), read_sample() -> Sample
so the rest of the program never needs to know whether real hardware is attached.
"""
from __future__ import annotations

import glob
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .config import DRATE_CODES, PGA_CODES, AppConfig, DAQConfig
from .processing import BeamModel, strain_to_ratio

log = logging.getLogger(__name__)
NAN = float("nan")


class HardwareError(RuntimeError):
    """Raised when the ADC or a sensor cannot be reached or misbehaves."""


@dataclass
class Sample:
    t: float          # seconds since acquisition start
    v_strain: float   # AIN0 - AIN1 [V] (NaN if channel disabled)
    v_pzt: float      # AIN2 - AIN3 [V] (NaN if channel disabled)
    temp_c: float = NAN


def hardware_libraries_available() -> Tuple[bool, str]:
    """Check whether spidev and lgpio can be imported (only true on the Pi)."""
    missing = []
    for name in ("spidev", "lgpio"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        return False, "Missing Python modules: " + ", ".join(missing)
    return True, "spidev and lgpio found"


def _channels(mode: str) -> Tuple[bool, bool]:
    return ("strain" in mode, "pzt" in mode)


# ====================================================================== ADS1256
class ADS1256FrontEnd:
    """Texas Instruments ADS1256 on the Strain-PZT DAQ HAT.

    Wiring (BCM): SPI0 MOSI/MISO/SCLK, CS = GPIO22 (driven manually),
    DRDY = GPIO17, RESET = GPIO4. Strain on AIN0-AIN1, PZT on AIN2-AIN3.
    """

    is_simulation = False

    # commands
    WAKEUP, RDATA, SDATAC, SELFCAL, SYNC, RESET = 0x00, 0x01, 0x0F, 0xF0, 0xFC, 0xFE
    WREG, RREG = 0x50, 0x10
    # registers
    REG_STATUS, REG_MUX, REG_ADCON, REG_DRATE = 0x00, 0x01, 0x02, 0x03
    CHIP_ID = 3  # STATUS[7:4]

    MUX_STRAIN = (0 << 4) | 1  # AIN0 - AIN1
    MUX_PZT = (2 << 4) | 3     # AIN2 - AIN3

    def __init__(self, daq: DAQConfig, drdy_timeout_s: float = 0.5):
        self.cfg = daq
        self.drdy_timeout_s = drdy_timeout_s
        self._spi = None
        self._lgpio = None
        self._chip = None
        self._t0 = 0.0
        self._lock = threading.Lock()
        self.use_strain, self.use_pzt = _channels(daq.channel_mode)

    # ------------------------------------------------------------ open / close
    def open(self) -> None:
        try:
            import lgpio  # type: ignore
            import spidev  # type: ignore
        except ImportError as exc:
            raise HardwareError(
                f"{exc}. Install with: sudo apt install python3-spidev python3-lgpio"
            ) from exc
        self._lgpio = lgpio
        self._chip = self._open_header_gpiochip(lgpio)
        c = self.cfg
        try:
            lgpio.gpio_claim_input(self._chip, c.pin_drdy)
            lgpio.gpio_claim_output(self._chip, c.pin_reset, 1)
            lgpio.gpio_claim_output(self._chip, c.pin_cs, 1)
        except Exception as exc:  # lgpio raises lgpio.error
            self.close()
            raise HardwareError(f"Cannot claim GPIO pins (busy or no permission): {exc}") from exc
        try:
            spi = spidev.SpiDev()
            spi.open(c.spi_bus, c.spi_device)
            spi.max_speed_hz = c.spi_speed_hz
            spi.mode = 0b01  # CPOL=0, CPHA=1
            self._spi = spi
        except (OSError, FileNotFoundError) as exc:
            self.close()
            raise HardwareError(
                f"Cannot open /dev/spidev{c.spi_bus}.{c.spi_device}: {exc}. Enable SPI with raspi-config."
            ) from exc
        try:
            self._initialise()
        except Exception:
            self.close()
            raise
        self._t0 = time.perf_counter()

    def close(self) -> None:
        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._spi = None
        if self._chip is not None and self._lgpio is not None:
            for pin in (self.cfg.pin_drdy, self.cfg.pin_reset, self.cfg.pin_cs):
                try:
                    self._lgpio.gpio_free(self._chip, pin)
                except Exception:
                    pass
            try:
                self._lgpio.gpiochip_close(self._chip)
            except Exception:
                pass
            self._chip = None

    @staticmethod
    def _open_header_gpiochip(lgpio) -> int:
        """Find the gpiochip that drives the 40-pin header (RP1 on Pi 5, BCM283x/2711 on older Pis)."""
        fallback = None
        for n in range(0, 8):
            try:
                h = lgpio.gpiochip_open(n)
            except Exception:
                continue
            try:
                info = lgpio.gpio_get_chip_info(h)
                label = " ".join(str(v) for v in info[1:]).lower() if isinstance(info, (tuple, list)) else str(info).lower()
            except Exception:
                label = ""
            if "rp1" in label or "bcm2" in label:
                if fallback is not None:
                    lgpio.gpiochip_close(fallback)
                return h
            if fallback is None:
                fallback = h
            else:
                lgpio.gpiochip_close(h)
        if fallback is not None:
            log.warning("Header gpiochip label not recognised; using the first gpiochip found.")
            return fallback
        raise HardwareError("No /dev/gpiochip* device could be opened.")

    # ------------------------------------------------------------ low level
    def _cs(self, level: int) -> None:
        self._lgpio.gpio_write(self._chip, self.cfg.pin_cs, level)

    def _command(self, cmd: int) -> None:
        self._cs(0)
        self._spi.xfer2([cmd])
        self._cs(1)

    def _write_reg(self, reg: int, value: int) -> None:
        self._cs(0)
        self._spi.xfer2([self.WREG | reg, 0x00, value & 0xFF])
        self._cs(1)

    def _read_reg(self, reg: int) -> int:
        self._cs(0)
        self._spi.xfer2([self.RREG | reg, 0x00])
        _delay_us(10)  # t6 >= 50 tCLKIN = 6.5 us
        value = self._spi.xfer2([0xFF])[0]
        self._cs(1)
        return value

    def _wait_drdy(self) -> None:
        deadline = time.perf_counter() + self.drdy_timeout_s
        read = self._lgpio.gpio_read
        chip, pin = self._chip, self.cfg.pin_drdy
        while read(chip, pin):
            if time.perf_counter() > deadline:
                raise HardwareError("ADS1256 DRDY timeout: check the crystal, power and the DRDY wire (GPIO17).")

    def _read_code(self) -> int:
        self._cs(0)
        self._spi.xfer2([self.RDATA])
        _delay_us(10)
        b = self._spi.xfer2([0xFF, 0xFF, 0xFF])
        self._cs(1)
        code = (b[0] << 16) | (b[1] << 8) | b[2]
        return code - (1 << 24) if code & 0x800000 else code

    def _initialise(self) -> None:
        lg, c = self._lgpio, self.cfg
        lg.gpio_write(self._chip, c.pin_reset, 0)
        time.sleep(0.001)
        lg.gpio_write(self._chip, c.pin_reset, 1)
        time.sleep(0.05)
        self._wait_drdy()
        self._command(self.SDATAC)
        chip_id = self._read_reg(self.REG_STATUS) >> 4
        if chip_id != self.CHIP_ID:
            raise HardwareError(
                f"ADS1256 not detected (STATUS ID = {chip_id}, expected {self.CHIP_ID}). "
                "Check SPI wiring, CS on GPIO22 and the 3.3 V / 5 V supplies."
            )
        self._write_reg(self.REG_STATUS, 0x04)            # MSB first, auto-cal on, input buffer OFF
        self._write_reg(self.REG_ADCON, PGA_CODES[c.pga_gain])  # CLKOUT off, sensor detect off
        self._write_reg(self.REG_DRATE, DRATE_CODES[c.data_rate_sps])
        self._write_reg(self.REG_MUX, self.MUX_STRAIN if self.use_strain else self.MUX_PZT)
        self._wait_drdy()
        self._command(self.SELFCAL)
        self._wait_drdy()
        log.info("ADS1256 ready: %d SPS, PGA %d, channels %s", c.data_rate_sps, c.pga_gain, c.channel_mode)

    def _read_channel(self, mux: int) -> float:
        """Switch the input multiplexer and return one fully settled conversion [V]."""
        self._write_reg(self.REG_MUX, mux)
        self._command(self.SYNC)
        self._command(self.WAKEUP)
        self._wait_drdy()
        return self._code_to_volts(self._read_code())

    def _code_to_volts(self, code: int) -> float:
        return code / 0x7FFFFF * (2.0 * self.cfg.vref_v / self.cfg.pga_gain)

    # ------------------------------------------------------------ public
    def read_sample(self) -> Sample:
        if self._spi is None:
            raise HardwareError("ADS1256 is not open.")
        with self._lock:
            if self.use_strain and self.use_pzt:
                t1 = time.perf_counter()
                vs = self._read_channel(self.MUX_STRAIN)
                vp = self._read_channel(self.MUX_PZT)
                t = 0.5 * (t1 + time.perf_counter()) - self._t0
            else:
                # single channel: the multiplexer stays put, just read every conversion
                self._wait_drdy()
                v = self._code_to_volts(self._read_code())
                t = time.perf_counter() - self._t0
                vs, vp = (v, NAN) if self.use_strain else (NAN, v)
        return Sample(t=t, v_strain=vs, v_pzt=vp)


def _delay_us(us: float) -> None:
    end = time.perf_counter() + us * 1e-6
    while time.perf_counter() < end:
        pass


# ====================================================================== DS18B20
class DS18B20:
    """DS18B20 via the Linux w1-gpio / w1-therm drivers (sysfs)."""

    def __init__(self, device_glob: str = "/sys/bus/w1/devices/28-*"):
        self.device_glob = device_glob
        self.device: Optional[str] = None
        self._find()

    def _find(self) -> None:
        devices = sorted(glob.glob(self.device_glob))
        self.device = devices[0] if devices else None

    @property
    def available(self) -> bool:
        return self.device is not None

    def read_celsius(self) -> float:
        """Blocking read (~750 ms). Returns NaN on any error."""
        if self.device is None:
            self._find()
            if self.device is None:
                return NAN
        try:
            try:
                with open(f"{self.device}/temperature", encoding="ascii") as fh:
                    value = int(fh.read().strip()) / 1000.0
            except FileNotFoundError:
                with open(f"{self.device}/w1_slave", encoding="ascii") as fh:
                    lines = fh.read().splitlines()
                if len(lines) < 2 or not lines[0].strip().endswith("YES") or "t=" not in lines[1]:
                    return NAN
                value = int(lines[1].split("t=")[1]) / 1000.0
        except (OSError, ValueError):
            return NAN
        if value == 85.0 or not -55.0 <= value <= 125.0:  # 85 degC = power-on value, not a reading
            return NAN
        return value


# ====================================================================== simulator
class SimulatedFrontEnd:
    """Physics-based stand-in for the HAT: lets you run and test the whole program without hardware.

    It models the beam (static load + damped first-mode vibration), the half/quarter/full bridge,
    the INA333 gain, the PZT charge amplifier and a slowly drifting room temperature.
    """

    is_simulation = True

    def __init__(self, config: AppConfig, seed: Optional[int] = None, realtime: bool = True):
        self.cfg = config
        self.realtime = realtime
        self.rng = np.random.default_rng(seed)
        self.beam = BeamModel(config.beam)
        self.f1 = self.beam.natural_frequency_hz()
        self.zeta = 0.012
        self.gf_error = 1.06                   # true GF is 6 % higher than configured -> calibration fixes it
        self.alpha_true_ue_per_c = 1.5         # apparent thermal strain
        self.pzt_nc_per_ue = 2.0               # charge sensitivity of the bonded plate
        self.bridge_offset_ratio = 2.0e-5      # bridge imbalance
        self.pzt_offset_v = 1.5e-3
        self.tap_interval_s = 6.0
        self.tap_amplitude_ue = 3.0
        self.use_strain, self.use_pzt = _channels(config.daq.channel_mode)
        n_ch = int(self.use_strain) + int(self.use_pzt)
        per_conv = 1.0 / config.daq.data_rate_sps + (3e-4 if n_ch > 1 else 1e-4)
        self.period = max(n_ch * per_conv, 1e-3)
        self._lock = threading.Lock()
        self._mass_g = 0.0
        self._static_ue = 0.0
        self._target_prev_ue = 0.0
        self._events: List[Tuple[float, float]] = []
        self._next_tap = 2.0
        self._t0 = 0.0
        self._k = 0
        self._last_t = 0.0
        self._open = False

    # ------------------------------------------------------------ control
    def set_mass(self, mass_g: float) -> None:
        with self._lock:
            self._mass_g = max(0.0, float(mass_g))

    @property
    def mass_g(self) -> float:
        return self._mass_g

    def tap(self, amplitude_ue: Optional[float] = None) -> None:
        with self._lock:
            self._events.append((self._last_t, self.tap_amplitude_ue if amplitude_ue is None else amplitude_ue))

    def open(self) -> None:
        self._t0 = time.perf_counter()
        self._k = 0
        self._last_t = 0.0
        self._static_ue = float(self.beam.strain_from_mass(self._mass_g)) * 1e6
        self._target_prev_ue = self._static_ue
        self._events = []
        self._next_tap = 2.0
        self._open = True
        log.info("Simulator started: f1 = %.1f Hz, sample period %.2f ms", self.f1, self.period * 1e3)

    def close(self) -> None:
        self._open = False

    # ------------------------------------------------------------ physics
    def temperature_at(self, t: float) -> float:
        return 22.0 + 0.8 * math.sin(2 * math.pi * t / 120.0) + 0.3 * math.sin(2 * math.pi * t / 37.0)

    def read_temperature(self) -> float:
        """Mimics the DS18B20: 12-bit resolution (0.0625 degC)."""
        t = time.perf_counter() - self._t0 if self.realtime else self._last_t
        return round(self.temperature_at(t) / 0.0625) * 0.0625

    def _dynamic_ue(self, t: float) -> float:
        w = 2 * math.pi * self.f1
        wd = w * math.sqrt(1 - self.zeta ** 2)
        horizon = 8.0 / (self.zeta * w)
        total = 0.0
        keep = []
        for te, amp in self._events:
            dt = t - te
            if dt < 0:
                keep.append((te, amp))
                continue
            if dt <= horizon:
                keep.append((te, amp))
                total += amp * math.exp(-self.zeta * w * dt) * math.sin(wd * dt)
        self._events = keep
        return total

    def read_sample(self) -> Sample:
        if not self._open:
            raise HardwareError("Simulator is not open.")
        if self.realtime:
            target = self._t0 + self._k * self.period
            now = time.perf_counter()
            if target > now:
                time.sleep(target - now)
            elif now - target > 0.25:          # fell behind (GUI busy): resynchronise
                self._k = int((now - self._t0) / self.period)
            t = max(time.perf_counter(), target) - self._t0
        else:
            t = self._k * self.period
        self._k += 1
        dt = max(t - self._last_t, 0.0)
        self._last_t = t

        with self._lock:
            target_ue = float(self.beam.strain_from_mass(self._mass_g)) * 1e6
            if target_ue != self._target_prev_ue:      # load changed: the beam rings
                self._events.append((t, 0.3 * (target_ue - self._target_prev_ue)))
                self._target_prev_ue = target_ue
            alpha = 1.0 - math.exp(-dt / 0.05) if dt > 0 else 0.0
            self._static_ue += (target_ue - self._static_ue) * alpha
            if t >= self._next_tap:
                self._events.append((t, self.tap_amplitude_ue))
                self._next_tap = t + self.tap_interval_s
            dyn = self._dynamic_ue(t)

        s, p = self.cfg.strain, self.cfg.pzt
        temp = self.temperature_at(t)
        eps_ue = self._static_ue + dyn + self.alpha_true_ue_per_c * (temp - 22.0)
        eps = eps_ue * 1e-6 * self.gf_error * s.polarity
        ratio = float(strain_to_ratio(eps, s.gauge_factor, s.bridge_mode)) + self.bridge_offset_ratio
        v_strain = ratio * s.ina_gain * s.excitation_v + self.rng.normal(0.0, 4e-6)
        v_strain = float(np.clip(v_strain, -2.45, 2.45))

        charge_nc = self.pzt_nc_per_ue * dyn
        v_pzt = -charge_nc / p.feedback_cap_nf * p.polarity + self.pzt_offset_v + self.rng.normal(0.0, 5e-5)
        v_pzt = float(np.clip(v_pzt, -2.45, 2.45))

        return Sample(
            t=t,
            v_strain=v_strain if self.use_strain else NAN,
            v_pzt=v_pzt if self.use_pzt else NAN,
        )
