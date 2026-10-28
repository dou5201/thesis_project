"""Signal processing and beam mechanics. Pure functions, no hardware, no GUI."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

from .config import STANDARD_GRAVITY, AppConfig, BeamConfig

CLIP_LIMIT_V = 2.40  # |AINp - AINn| above this means the amplifier is at its rail

PROCESSED_COLUMNS = (
    "t_s", "v_strain_V", "v_pzt_V", "temp_C",
    "bridge_ratio", "strain_raw_ue", "strain_tc_ue", "strain_ue",
    "deflection_mm", "force_N", "load_g",
    "pzt_mV", "pzt_charge_nC", "strain_clip", "pzt_clip",
)
RAW_COLUMNS = ("t_s", "v_strain_V", "v_pzt_V", "temp_C")


# ---------------------------------------------------------------------- conversions
def code_to_volts(code, vref: float, pga: int):
    """ADS1256 24-bit two's-complement code -> differential input voltage.
    Full scale is +-2*VREF/PGA."""
    return np.asarray(code, dtype=float) / 0x7FFFFF * (2.0 * vref / pga)


def bridge_ratio(v_diff, ina_gain: float, excitation_v: float):
    """ADC differential voltage (INA output - VREF) -> bridge output / excitation."""
    return np.asarray(v_diff, dtype=float) / (ina_gain * excitation_v)


def ratio_to_strain(ratio, gauge_factor: float, mode: str):
    """Bridge ratio -> strain (dimensionless) for this board's wiring.

    quarter: gauge between EXC+ and SIG+, R3 completes the arm  -> exact non-linear formula
    half:    gauge on top (EXC+..SIG+) and gauge below (SIG+..GND) -> linear
    full:    four active gauges, external bridge                 -> linear
    """
    r = np.asarray(ratio, dtype=float)
    if mode == "quarter":
        return -4.0 * r / (gauge_factor * (1.0 + 2.0 * r))
    if mode == "half":
        return -2.0 * r / gauge_factor
    if mode == "full":
        return -r / gauge_factor
    raise ValueError(f"Unknown bridge mode: {mode!r}")


def strain_to_ratio(strain, gauge_factor: float, mode: str):
    """Inverse of :func:`ratio_to_strain` (used by the simulator and tests)."""
    x = gauge_factor * np.asarray(strain, dtype=float)
    if mode == "quarter":
        return -x / (2.0 * (2.0 + x))
    if mode == "half":
        return -x / 2.0
    if mode == "full":
        return -x
    raise ValueError(f"Unknown bridge mode: {mode!r}")


# ---------------------------------------------------------------------- beam mechanics
class BeamModel:
    """Euler-Bernoulli cantilever, rectangular section, point load at the tip."""

    MODE1 = 1.8751040687  # first root of 1 + cos(bL) cosh(bL) = 0

    def __init__(self, cfg: BeamConfig):
        self.L = cfg.length_mm / 1000.0
        self.x = cfg.gauge_pos_mm / 1000.0
        self.b = cfg.width_mm / 1000.0
        self.h = cfg.thickness_mm / 1000.0
        self.E = cfg.youngs_modulus_gpa * 1e9
        self.rho = cfg.density_kg_m3
        if not 0 <= self.x < self.L:
            raise ValueError("Gauge position must lie between the clamp and the tip.")
        self.I = self.b * self.h ** 3 / 12.0
        self.A = self.b * self.h

    def strain_from_force(self, force_n):
        """Surface strain at the gauge for a tip force: e = F (L - x) (h/2) / (E I)."""
        return np.asarray(force_n, dtype=float) * (self.L - self.x) * (self.h / 2.0) / (self.E * self.I)

    def force_from_strain(self, strain):
        return np.asarray(strain, dtype=float) * self.E * self.I / ((self.L - self.x) * (self.h / 2.0))

    def strain_from_mass(self, mass_g):
        return self.strain_from_force(np.asarray(mass_g, dtype=float) / 1000.0 * STANDARD_GRAVITY)

    def tip_deflection_from_force(self, force_n):
        return np.asarray(force_n, dtype=float) * self.L ** 3 / (3.0 * self.E * self.I)

    def tip_deflection_from_strain(self, strain):
        """d_tip = 2 L^3 e / (3 h (L - x))."""
        return self.tip_deflection_from_force(self.force_from_strain(strain))

    def natural_frequency_hz(self) -> float:
        """First bending mode without tip mass."""
        return self.MODE1 ** 2 / (2.0 * math.pi * self.L ** 2) * math.sqrt(self.E * self.I / (self.rho * self.A))


# ---------------------------------------------------------------------- processor
@dataclass
class TareState:
    ratio0: float = 0.0
    v_pzt0: float = 0.0
    temp0: float = float("nan")


class SignalProcessor:
    """Converts raw ADC voltages into engineering values, batch-wise (numpy)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.beam = BeamModel(config.beam)
        self.tare = TareState()

    def set_tare(self, ratio0: float, v_pzt0: float, temp0: float) -> None:
        self.tare = TareState(
            ratio0=float(ratio0) if np.isfinite(ratio0) else 0.0,
            v_pzt0=float(v_pzt0) if np.isfinite(v_pzt0) else 0.0,
            temp0=float(temp0),
        )

    def clear_tare(self) -> None:
        self.tare = TareState()

    def process_batch(self, t, v_strain, v_pzt, temp_c) -> Dict[str, np.ndarray]:
        cfg = self.config
        s, p = cfg.strain, cfg.pzt
        t = np.asarray(t, dtype=float)
        v_strain = np.asarray(v_strain, dtype=float)
        v_pzt = np.asarray(v_pzt, dtype=float)
        temp_c = np.broadcast_to(np.asarray(temp_c, dtype=float), t.shape).astype(float)

        ratio = bridge_ratio(v_strain, s.ina_gain, s.excitation_v)
        strain_raw = s.polarity * ratio_to_strain(ratio - self.tare.ratio0, s.gauge_factor, s.bridge_mode) * 1e6

        strain_tc = strain_raw.copy()
        tc = cfg.temperature
        if tc.compensation_enabled and np.isfinite(self.tare.temp0):
            dT = temp_c - self.tare.temp0
            ok = np.isfinite(dT)
            strain_tc[ok] = strain_raw[ok] - tc.alpha_ue_per_c * dT[ok]

        if cfg.calibration.enabled:
            strain = np.polyval(cfg.calibration.coefficients, strain_tc)
        else:
            strain = strain_tc

        eps = strain * 1e-6
        force = self.beam.force_from_strain(eps)
        pzt_v = (v_pzt - self.tare.v_pzt0) * p.polarity
        return {
            "t_s": t,
            "v_strain_V": v_strain,
            "v_pzt_V": v_pzt,
            "temp_C": temp_c,
            "bridge_ratio": ratio,
            "strain_raw_ue": strain_raw,
            "strain_tc_ue": strain_tc,
            "strain_ue": strain,
            "deflection_mm": self.beam.tip_deflection_from_strain(eps) * 1e3,
            "force_N": force,
            "load_g": force / STANDARD_GRAVITY * 1000.0,
            "pzt_mV": pzt_v * 1e3,
            "pzt_charge_nC": -pzt_v * p.feedback_cap_nf,  # Vout = VREF - Q/Cf
            "strain_clip": (np.abs(v_strain) > CLIP_LIMIT_V).astype(float),
            "pzt_clip": (np.abs(v_pzt) > CLIP_LIMIT_V).astype(float),
        }


# ---------------------------------------------------------------------- analysis helpers
def amplitude_spectrum(t: Sequence[float], x: Sequence[float]) -> Tuple[np.ndarray, np.ndarray, float]:
    """Single-sided amplitude spectrum of a possibly non-uniformly sampled signal.

    The signal is resampled onto a uniform grid, mean-removed and Hann-windowed.
    Returns (frequency_Hz, amplitude, sample_rate_Hz)."""
    t = np.asarray(t, dtype=float)
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(t) & np.isfinite(x)
    t, x = t[ok], x[ok]
    if t.size < 16:
        raise ValueError("Need at least 16 valid samples for a spectrum.")
    order = np.argsort(t)
    t, x = t[order], x[order]
    duration = t[-1] - t[0]
    if duration <= 0:
        raise ValueError("Time stamps do not increase.")
    n = t.size
    fs = (n - 1) / duration
    tu = np.linspace(t[0], t[-1], n)
    xu = np.interp(tu, t, x)
    xu = xu - xu.mean()
    w = np.hanning(n)
    spec = np.fft.rfft(xu * w)
    amp = 2.0 * np.abs(spec) / w.sum()
    f = np.fft.rfftfreq(n, d=1.0 / fs)
    return f, amp, fs


def dominant_frequency(f: np.ndarray, amp: np.ndarray, f_min: float = 1.0) -> float:
    """Peak frequency above f_min with parabolic interpolation between bins."""
    f = np.asarray(f)
    amp = np.asarray(amp)
    idx = np.where(f >= f_min)[0]
    if idx.size < 3:
        return float("nan")
    k = idx[np.argmax(amp[idx])]
    if 0 < k < len(amp) - 1:
        a, b, c = amp[k - 1], amp[k], amp[k + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
        return float(f[k] + delta * (f[1] - f[0]))
    return float(f[k])


def fit_polynomial(x: Sequence[float], y: Sequence[float], order: int) -> Tuple[np.ndarray, float]:
    """Least-squares polynomial (numpy.polyval order) and coefficient of determination."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.size < order + 1:
        raise ValueError(f"Need at least {order + 1} points for an order-{order} fit (have {x.size}).")
    coeffs = np.polyfit(x, y, order)
    resid = y - np.polyval(coeffs, x)
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum(resid ** 2)) / ss_tot if ss_tot > 0 else float("nan")
    return coeffs, r2


def window_mean(values: np.ndarray, default: Optional[float] = float("nan")) -> float:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    return float(values.mean()) if values.size else default
