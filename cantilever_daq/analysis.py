"""Offline analysis and all plotting (pandas + seaborn on matplotlib Figures).

Every plot function draws into a matplotlib Figure that the caller owns, so the same code
works inside the Tkinter GUI and in headless scripts / tests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.figure import Figure

from .config import AppConfig
from .processing import BeamModel, amplitude_spectrum, dominant_frequency, fit_polynomial

SUMMARY_COLUMNS = ("strain_ue", "deflection_mm", "load_g", "pzt_mV", "pzt_charge_nC", "temp_C")
COLORS = {"strain": "#1f6f4a", "defl": "#2b5d9b", "pzt": "#b5541b", "temp": "#8e3b8e", "theory": "#555555"}


def apply_theme() -> None:
    sns.set_theme(style="whitegrid", context="notebook", palette="deep")


# ---------------------------------------------------------------------- loading
def load_recording(path) -> Tuple[pd.DataFrame, Optional[Dict]]:
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)
    if "t_s" not in df.columns:
        raise ValueError(f"{path.name} is not a recording from this program (no 't_s' column).")
    meta = None
    meta_path = path.with_suffix(".json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
    return df, meta


def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in SUMMARY_COLUMNS if c in df.columns and df[c].notna().any()]
    if not cols:
        return pd.DataFrame()
    table = df[cols].describe().T
    table["range"] = table["max"] - table["min"]
    return table[["count", "mean", "std", "min", "max", "range"]]


# ---------------------------------------------------------------------- analyses
@dataclass
class SpectrumResult:
    f: np.ndarray
    amp: np.ndarray
    fs: float
    peak_hz: float
    column: str


def spectrum(df: pd.DataFrame, column: str = "pzt_mV") -> SpectrumResult:
    if column not in df.columns or not df[column].notna().any():
        raise ValueError(f"Column '{column}' has no data.")
    f, amp, fs = amplitude_spectrum(df["t_s"].to_numpy(), df[column].to_numpy())
    return SpectrumResult(f, amp, fs, dominant_frequency(f, amp, f_min=2.0), column)


@dataclass
class DriftResult:
    alpha_ue_per_c: float
    intercept_ue: float
    r2: float
    n: int

    MIN_R2 = 0.8

    @property
    def reliable(self) -> bool:
        """True when temperature explains most of the strain change (i.e. the beam was unloaded)."""
        return bool(np.isfinite(self.r2) and self.r2 >= self.MIN_R2)


def temperature_drift(df: pd.DataFrame, strain_col: str = "strain_raw_ue") -> DriftResult:
    """Apparent strain per degC from a recording of the UNLOADED beam."""
    if "temp_C" not in df.columns or strain_col not in df.columns:
        raise ValueError("Recording needs temp_C and strain columns.")
    d = df[["temp_C", strain_col]].dropna()
    if d["temp_C"].nunique() < 3:
        raise ValueError("Temperature barely changed in this recording: need a wider temperature range.")
    coeffs, r2 = fit_polynomial(d["temp_C"], d[strain_col], 1)
    return DriftResult(float(coeffs[0]), float(coeffs[1]), r2, len(d))


def downsample(df: pd.DataFrame, max_points: int = 4000) -> pd.DataFrame:
    if len(df) <= max_points:
        return df
    return df.iloc[:: int(np.ceil(len(df) / max_points))]


# ---------------------------------------------------------------------- plots
def plot_analysis(fig: Figure, df: pd.DataFrame, config: AppConfig,
                  spec: Optional[SpectrumResult] = None, drift: Optional[DriftResult] = None) -> None:
    """2 x 2 overview: time series, distribution, PZT spectrum, strain vs temperature."""
    fig.clear()
    axs = fig.subplots(2, 2)
    d = downsample(df)

    ax = axs[0, 0]
    if "strain_ue" in d and d["strain_ue"].notna().any():
        sns.lineplot(data=d, x="t_s", y="strain_ue", ax=ax, color=COLORS["strain"], linewidth=0.9)
    ax.set(title="Strain over time", xlabel="Time [s]", ylabel="Strain [µε]")
    if "temp_C" in d and d["temp_C"].notna().any():
        ax2 = ax.twinx()
        ax2.plot(d["t_s"], d["temp_C"], color=COLORS["temp"], linewidth=0.9, alpha=0.8)
        ax2.set_ylabel("Temperature [°C]", color=COLORS["temp"])
        ax2.grid(False)

    ax = axs[0, 1]
    if "strain_ue" in df and df["strain_ue"].notna().any():
        sns.histplot(df["strain_ue"].dropna(), bins=60, kde=True, ax=ax, color=COLORS["strain"])
    ax.set(title="Strain distribution", xlabel="Strain [µε]", ylabel="Count")

    ax = axs[1, 0]
    f1 = BeamModel(config.beam).natural_frequency_hz()
    if spec is not None:
        mask = spec.f <= min(spec.fs / 2, max(5 * f1, 50))
        ax.plot(spec.f[mask], spec.amp[mask], color=COLORS["pzt"], linewidth=0.9)
        if np.isfinite(spec.peak_hz):
            ax.axvline(spec.peak_hz, color=COLORS["pzt"], linestyle="--", linewidth=1)
            ax.annotate(f"measured {spec.peak_hz:.1f} Hz", (spec.peak_hz, ax.get_ylim()[1] * 0.9),
                        xytext=(5, 0), textcoords="offset points", color=COLORS["pzt"])
        ax.axvline(f1, color=COLORS["theory"], linestyle=":", linewidth=1)
        ax.annotate(f"theory {f1:.1f} Hz", (f1, ax.get_ylim()[1] * 0.75), xytext=(5, 0),
                    textcoords="offset points", color=COLORS["theory"])
        ax.set(title=f"Spectrum of {spec.column} (fs ≈ {spec.fs:.0f} Hz)", xlabel="Frequency [Hz]", ylabel="Amplitude")
    else:
        ax.text(0.5, 0.5, "No PZT data", ha="center", va="center", transform=ax.transAxes)
        ax.set(title="PZT spectrum")

    ax = axs[1, 1]
    if drift is not None and "temp_C" in d:
        dd = d[["temp_C", "strain_raw_ue"]].dropna()
        sns.regplot(data=dd, x="temp_C", y="strain_raw_ue", ax=ax, scatter_kws={"s": 6, "alpha": 0.4},
                    line_kws={"color": COLORS["temp"]}, color=COLORS["strain"], ci=None)
        note = "" if drift.reliable else "\nUNRELIABLE: was the beam loaded?"
        ax.set(xlabel="Temperature [°C]", ylabel="Raw strain [µε]")
        ax.set_title(f"Thermal drift: α = {drift.alpha_ue_per_c:.2f} µε/°C, R² = {drift.r2:.2f}{note}",
                     color="#222222" if drift.reliable else "#b00020", fontsize=10)
    else:
        ax.text(0.5, 0.5, "No temperature data\n(or too little variation)", ha="center", va="center",
                transform=ax.transAxes)
        ax.set(title="Strain vs temperature")
    fig.subplots_adjust(left=0.08, right=0.97, top=0.92, bottom=0.08, hspace=0.45, wspace=0.58)


def plot_calibration(fig: Figure, points: Sequence, beam: BeamModel,
                     coefficients: Optional[Sequence[float]] = None) -> None:
    """Measured vs theoretical strain with the fitted correction, plus residuals."""
    fig.clear()
    ax, axr = fig.subplots(2, 1, gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    if not points:
        ax.text(0.5, 0.5, "Capture calibration points:\nput a known mass on the tip, wait, press Capture",
                ha="center", va="center", transform=ax.transAxes)
        ax.set(title="Calibration", ylabel="Theoretical strain [µε]")
        axr.set(xlabel="Measured strain [µε]", ylabel="Residual [µε]")
        fig.subplots_adjust(left=0.13, right=0.97, top=0.93, bottom=0.11, hspace=0.08)
        return
    df = pd.DataFrame([{"mass_g": p.mass_g, "measured_ue": p.measured_ue, "theory_ue": p.theory_ue,
                        "std_ue": p.std_ue} for p in points])
    sns.scatterplot(data=df, x="measured_ue", y="theory_ue", hue="mass_g", palette="viridis", s=60, ax=ax, zorder=3)
    ax.errorbar(df["measured_ue"], df["theory_ue"], xerr=df["std_ue"], fmt="none", ecolor="#888", zorder=2)
    lo, hi = float(df["measured_ue"].min()), float(df["measured_ue"].max())
    span = max(hi - lo, 1.0)
    xs = np.linspace(lo - 0.1 * span, hi + 0.1 * span, 200)
    ax.plot(xs, xs, color=COLORS["theory"], linestyle=":", label="ideal (no correction)")
    if coefficients is not None:
        ax.plot(xs, np.polyval(coefficients, xs), color=COLORS["strain"], label="fitted correction")
        resid = df["theory_ue"] - np.polyval(coefficients, df["measured_ue"])
        axr.axhline(0, color="#999", linewidth=0.8)
        axr.scatter(df["measured_ue"], resid, color=COLORS["strain"], s=25)
    ax.legend(loc="upper left", fontsize=8, title="mass [g]", title_fontsize=8)
    ax.set(title="Calibration: measured vs beam theory", ylabel="Theoretical strain [µε]")
    axr.set(xlabel="Measured strain [µε]", ylabel="Residual [µε]")
    fig.subplots_adjust(left=0.13, right=0.97, top=0.93, bottom=0.11, hspace=0.08)
