"""Configuration model: every tunable number of the experiment lives here.

The configuration is a tree of dataclasses that can be saved to and loaded from JSON.
Defaults match the Strain-PZT DAQ HAT rev 1.3 hardware.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

# ADS1256 DRATE register codes (datasheet table 13)
DRATE_CODES: Dict[int, int] = {
    30000: 0xF0, 15000: 0xE0, 7500: 0xD0, 3750: 0xC0, 2000: 0xB0, 1000: 0xA1,
    500: 0x92, 100: 0x82, 60: 0x72, 50: 0x63, 30: 0x53, 25: 0x43, 15: 0x33,
    10: 0x23, 5: 0x13,
}
# ADS1256 ADCON PGA bits
PGA_CODES: Dict[int, int] = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5, 64: 6}
BRIDGE_MODES = ("quarter", "half", "full")
CHANNEL_MODES = ("strain+pzt", "strain", "pzt")
STRAIN_UNITS = ("auto", "V", "ue")      # data file: bridge voltage or microstrain
PZT_UNITS = ("auto", "V", "mV")
STANDARD_GRAVITY = 9.80665


@dataclass
class BeamConfig:
    """Cantilever geometry and material (defaults: 25 mm square EN AW-6082-T6 bar)."""
    length_mm: float = 500.0          # clamp edge -> load point (tip)
    gauge_pos_mm: float = 50.0        # clamp edge -> centre of the strain gauge
    width_mm: float = 25.0
    thickness_mm: float = 25.0        # bending direction
    youngs_modulus_gpa: float = 70.0  # aluminium 6082-T6
    density_kg_m3: float = 2700.0


@dataclass
class StrainConfig:
    """Bridge and amplifier settings for the KSPB-1-350-E4 semiconductor gauge."""
    gauge_factor: float = 110.0       # take the exact value from your gauge package label
    bridge_mode: str = "half"         # quarter (JP1+JP2), half (JP2), full (none)
    excitation_v: float = 2.5         # VEXC from the REF5025 buffer
    ina_gain: float = 10.09           # INA333: G = 1 + 100k / 11k
    polarity: int = 1                 # flip to -1 if tension reads negative


@dataclass
class PZTConfig:
    """Charge amplifier settings (U5, C26 = Cf)."""
    feedback_cap_nf: float = 10.0
    polarity: int = 1


@dataclass
class DAQConfig:
    """ADS1256 and Raspberry Pi wiring."""
    data_rate_sps: int = 2000
    pga_gain: int = 1
    channel_mode: str = "strain+pzt"
    vref_v: float = 2.5
    spi_bus: int = 0
    spi_device: int = 0
    spi_speed_hz: int = 1_000_000     # must stay below fCLKIN/4 = 1.92 MHz
    pin_drdy: int = 17                # BCM numbering
    pin_reset: int = 4
    pin_cs: int = 22
    plot_window_s: float = 10.0


@dataclass
class TemperatureConfig:
    """DS18B20 on 1-Wire (GPIO26, dtoverlay=w1-gpio,gpiopin=26)."""
    enabled: bool = True
    w1_device_glob: str = "/sys/bus/w1/devices/28-*"
    poll_interval_s: float = 1.0
    compensation_enabled: bool = False
    alpha_ue_per_c: float = 0.0       # apparent strain of the unloaded beam per degC


@dataclass
class FileSourceConfig:
    """Data file written by a logger (e.g. on the SD card), read live in "File" mode."""
    path: str = ""                    # CSV / TXT / XLSX file, or a folder (its newest file is followed)
    start_at_end: bool = False        # True: skip the rows already in the file, show only new ones
    poll_interval_s: float = 0.2      # how often to look for new rows
    time_column: str = "auto"         # "auto", a column name, a column number (first = 0) or "none"
    strain_column: str = "auto"
    strain_unit: str = "auto"         # V (raw bridge voltage AIN0-AIN1) or ue (microstrain)
    pzt_column: str = "auto"
    pzt_unit: str = "auto"            # V or mV
    temp_column: str = "auto"
    sample_rate_hz: float = 100.0     # only used when the file has no time column


@dataclass
class CalibrationConfig:
    """Polynomial mapping measured strain -> true strain (numpy.polyval order)."""
    enabled: bool = False
    coefficients: List[float] = field(default_factory=lambda: [1.0, 0.0])


@dataclass
class AppConfig:
    beam: BeamConfig = field(default_factory=BeamConfig)
    strain: StrainConfig = field(default_factory=StrainConfig)
    pzt: PZTConfig = field(default_factory=PZTConfig)
    daq: DAQConfig = field(default_factory=DAQConfig)
    temperature: TemperatureConfig = field(default_factory=TemperatureConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    file_source: FileSourceConfig = field(default_factory=FileSourceConfig)

    SECTIONS = ("beam", "strain", "pzt", "daq", "temperature", "calibration", "file_source")

    # ------------------------------------------------------------------ serialisation
    def to_dict(self) -> Dict[str, Any]:
        return {name: asdict(getattr(self, name)) for name in self.SECTIONS}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        cfg = cls()
        for name in cls.SECTIONS:
            section = getattr(cfg, name)
            values = data.get(name, {}) or {}
            for f in fields(section):
                if f.name in values:
                    default = getattr(section, f.name)
                    setattr(section, f.name, _coerce(values[f.name], default))
        return cfg

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def copy(self) -> "AppConfig":
        return AppConfig.from_dict(self.to_dict())

    # ------------------------------------------------------------------ validation
    def validate(self) -> List[str]:
        """Return a list of human-readable problems (empty list = valid)."""
        e: List[str] = []
        b, s, p, d, t = self.beam, self.strain, self.pzt, self.daq, self.temperature
        for name in ("length_mm", "width_mm", "thickness_mm", "youngs_modulus_gpa", "density_kg_m3"):
            if getattr(b, name) <= 0:
                e.append(f"Beam {name} must be > 0.")
        if not 0 <= b.gauge_pos_mm < b.length_mm:
            e.append("Gauge position must be between 0 and the beam length.")
        if s.gauge_factor <= 0:
            e.append("Gauge factor must be > 0.")
        if s.bridge_mode not in BRIDGE_MODES:
            e.append(f"Bridge mode must be one of {BRIDGE_MODES}.")
        if s.excitation_v <= 0 or s.ina_gain <= 0:
            e.append("Excitation voltage and INA gain must be > 0.")
        if s.polarity not in (1, -1) or p.polarity not in (1, -1):
            e.append("Polarity must be 1 or -1.")
        if p.feedback_cap_nf <= 0:
            e.append("PZT feedback capacitance must be > 0.")
        if d.data_rate_sps not in DRATE_CODES:
            e.append(f"Data rate must be one of {sorted(DRATE_CODES)}.")
        if d.pga_gain not in PGA_CODES:
            e.append(f"PGA gain must be one of {sorted(PGA_CODES)}.")
        if d.channel_mode not in CHANNEL_MODES:
            e.append(f"Channel mode must be one of {CHANNEL_MODES}.")
        if not 10_000 <= d.spi_speed_hz <= 1_920_000:
            e.append("SPI speed must be between 10 kHz and 1.92 MHz.")
        if d.vref_v <= 0 or d.plot_window_s <= 0:
            e.append("VREF and plot window must be > 0.")
        if t.poll_interval_s < 0.75:
            e.append("DS18B20 poll interval must be >= 0.75 s (12-bit conversion time).")
        f = self.file_source
        if f.strain_unit not in STRAIN_UNITS or f.pzt_unit not in PZT_UNITS:
            e.append(f"Data file units must be one of {STRAIN_UNITS} (strain) and {PZT_UNITS} (PZT).")
        if not 0.01 <= f.poll_interval_s <= 10 or f.sample_rate_hz <= 0:
            e.append("Data file poll interval must be 0.01-10 s and its sample rate > 0.")
        if len(self.calibration.coefficients) < 2:
            e.append("Calibration needs at least 2 coefficients.")
        return e


def _coerce(value: Any, default: Any) -> Any:
    """Convert a JSON value to the type of the dataclass default."""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(round(float(value)))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        return [float(v) for v in value]
    return str(value)
