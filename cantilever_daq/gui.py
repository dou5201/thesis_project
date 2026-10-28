"""Tkinter user interface. All measurement logic lives in ExperimentController; this module only
draws widgets, forwards button presses and refreshes the plots."""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Started directly ("python gui.py"): make the package importable and run as part of it.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "cantilever_daq"

import logging
import math
import tkinter as tk
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, Optional, Tuple

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from . import __version__
from .acquisition import ExperimentController
from .analysis import COLORS, apply_theme, load_recording, plot_analysis, plot_calibration, spectrum, \
    summary_table, temperature_drift
from .config import BRIDGE_MODES, CHANNEL_MODES, DRATE_CODES, PGA_CODES, AppConfig
from .hardware import HardwareError, hardware_libraries_available
from .processing import BeamModel

log = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = Path.home() / ".config" / "cantilever_daq" / "settings.json"
POLL_MS = 50
PLOT_MS = 120
READOUT_MS = 200
MAX_PLOT_POINTS = 1500

LIVE_TRACES = (  # key, axis label, colour, minimum y-span
    ("strain_ue", "Strain [µε]", COLORS["strain"], 1.0),
    ("deflection_mm", "Tip defl. [mm]", COLORS["defl"], 0.002),
    ("pzt_mV", "PZT [mV]", COLORS["pzt"], 5.0),
    ("temp_C", "Temp. [°C]", COLORS["temp"], 0.25),
)
READOUTS = (  # key, label, unit, format
    ("strain_ue", "Strain", "µε", "{:+.2f}"),
    ("deflection_mm", "Tip deflection", "mm", "{:+.4f}"),
    ("load_g", "Equivalent tip load", "g", "{:+.1f}"),
    ("pzt_mV", "PZT voltage", "mV", "{:+.1f}"),
    ("pzt_charge_nC", "PZT charge", "nC", "{:+.3f}"),
    ("temp_C", "Temperature", "°C", "{:.2f}"),
)
SETTING_LABELS = {
    "length_mm": "Length clamp → tip [mm]", "gauge_pos_mm": "Gauge position from clamp [mm]",
    "width_mm": "Width [mm]", "thickness_mm": "Thickness (bending) [mm]",
    "youngs_modulus_gpa": "Young's modulus [GPa]", "density_kg_m3": "Density [kg/m³]",
    "gauge_factor": "Gauge factor", "bridge_mode": "Bridge mode", "excitation_v": "Excitation [V]",
    "ina_gain": "INA333 gain", "polarity": "Polarity", "feedback_cap_nf": "Feedback capacitor Cf [nF]",
    "data_rate_sps": "ADS1256 data rate [SPS]", "pga_gain": "PGA gain", "channel_mode": "Channels",
    "vref_v": "VREF [V]", "spi_bus": "SPI bus", "spi_device": "SPI device", "spi_speed_hz": "SPI clock [Hz]",
    "pin_drdy": "DRDY pin (BCM)", "pin_reset": "RESET pin (BCM)", "pin_cs": "CS pin (BCM)",
    "plot_window_s": "Plot window [s]", "enabled": "Enabled", "w1_device_glob": "1-Wire device path",
    "poll_interval_s": "Poll interval [s]", "compensation_enabled": "Temperature compensation",
    "alpha_ue_per_c": "Thermal drift α [µε/°C]",
}
SECTION_TITLES = {"beam": "Beam", "strain": "Strain gauge bridge", "pzt": "PZT charge amplifier",
                  "daq": "ADC / Raspberry Pi", "temperature": "DS18B20 temperature"}
CHOICES = {
    ("strain", "bridge_mode"): BRIDGE_MODES,
    ("strain", "polarity"): (1, -1),
    ("pzt", "polarity"): (1, -1),
    ("daq", "channel_mode"): CHANNEL_MODES,
    ("daq", "data_rate_sps"): tuple(sorted(DRATE_CODES, reverse=True)),
    ("daq", "pga_gain"): tuple(sorted(PGA_CODES)),
}


class DAQApp:
    def __init__(self, root: tk.Tk, controller: ExperimentController, prefer_simulation: bool = False,
                 settings_path: Path = DEFAULT_SETTINGS_PATH):
        self.root = root
        self.ctl = controller
        self.settings_path = Path(settings_path)
        self._unsaved = False
        self._capture_job: Optional[str] = None
        self._last_fit = None
        self._analysis_df = None
        self._drift = None
        self._jobs: Dict[str, Optional[str]] = {}

        hw_ok, hw_msg = hardware_libraries_available()
        self.hw_message = hw_msg
        self.mode_var = tk.StringVar(value="simulation" if (prefer_simulation or not hw_ok) else "hardware")

        apply_theme()
        root.title(f"Cantilever Beam DAQ  {__version__}")
        root.geometry("1360x860")
        root.minsize(1100, 700)
        self._build_style()
        self._build_toolbar()
        self._build_notebook()
        self._build_statusbar()
        self._update_controls()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._schedule("poll", POLL_MS, self._poll)
        self._schedule("plot", PLOT_MS, self._refresh_plot)
        self._schedule("readout", READOUT_MS, self._refresh_readouts)
        self.set_status(f"Ready. {hw_msg}.")

    # ================================================================== layout
    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Value.TLabel", font=("DejaVu Sans", 20, "bold"))
        style.configure("Unit.TLabel", font=("DejaVu Sans", 10), foreground="#555555")
        style.configure("Name.TLabel", font=("DejaVu Sans", 10))
        style.configure("Warn.TLabel", foreground="#b00020", font=("DejaVu Sans", 10, "bold"))
        style.configure("Mode.TLabel", font=("DejaVu Sans", 10, "bold"))
        style.configure("Accent.TButton", font=("DejaVu Sans", 10, "bold"))

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(bar, text="Source:").pack(side=tk.LEFT)
        self.rb_hw = ttk.Radiobutton(bar, text="Hardware", variable=self.mode_var, value="hardware")
        self.rb_sim = ttk.Radiobutton(bar, text="Simulation", variable=self.mode_var, value="simulation")
        self.rb_hw.pack(side=tk.LEFT, padx=(4, 0))
        self.rb_sim.pack(side=tk.LEFT, padx=(4, 12))
        self.btn_start = ttk.Button(bar, text="▶ Start", style="Accent.TButton", command=self.on_start)
        self.btn_stop = ttk.Button(bar, text="■ Stop", command=self.on_stop)
        self.btn_tare = ttk.Button(bar, text="Tare (zero)", command=self.on_tare)
        self.btn_record = ttk.Button(bar, text="● Record", command=self.on_record)
        self.btn_save = ttk.Button(bar, text="Save recording…", command=self.on_save_recording)
        for b in (self.btn_start, self.btn_stop, self.btn_tare, self.btn_record, self.btn_save):
            b.pack(side=tk.LEFT, padx=3)
        self.mode_label = ttk.Label(bar, text="", style="Mode.TLabel")
        self.mode_label.pack(side=tk.RIGHT)

    def _build_notebook(self) -> None:
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))
        self.tab_live = ttk.Frame(self.nb)
        self.tab_cal = ttk.Frame(self.nb)
        self.tab_ana = ttk.Frame(self.nb)
        self.tab_set = ttk.Frame(self.nb)
        self.nb.add(self.tab_live, text="  Live  ")
        self.nb.add(self.tab_cal, text="  Calibration  ")
        self.nb.add(self.tab_ana, text="  Analysis  ")
        self.nb.add(self.tab_set, text="  Settings  ")
        self._build_live_tab()
        self._build_calibration_tab()
        self._build_analysis_tab()
        self._build_settings_tab()

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 3))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="")
        self.info_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Label(bar, textvariable=self.info_var).pack(side=tk.RIGHT)

    @staticmethod
    def _make_canvas(parent, fig: Figure) -> FigureCanvasTkAgg:
        canvas = FigureCanvasTkAgg(fig, master=parent)
        toolbar = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side=tk.BOTTOM, fill=tk.X)
        canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        return canvas

    # ------------------------------------------------------------------ live tab
    def _build_live_tab(self) -> None:
        left = ttk.Frame(self.tab_live, padding=10)
        left.pack(side=tk.LEFT, fill=tk.Y)
        right = ttk.Frame(self.tab_live)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.readout_vars: Dict[str, tk.StringVar] = {}
        for i, (key, name, unit, _fmt) in enumerate(READOUTS):
            ttk.Label(left, text=name, style="Name.TLabel").grid(row=2 * i, column=0, columnspan=2, sticky="w", pady=(6, 0))
            var = tk.StringVar(value="—")
            self.readout_vars[key] = var
            ttk.Label(left, textvariable=var, style="Value.TLabel", width=9, anchor="e").grid(row=2 * i + 1, column=0, sticky="e")
            ttk.Label(left, text=unit, style="Unit.TLabel").grid(row=2 * i + 1, column=1, sticky="w", padx=(4, 0))
        r = 2 * len(READOUTS)
        self.rate_var = tk.StringVar(value="Rate: —")
        ttk.Label(left, textvariable=self.rate_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.clip_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.clip_var, style="Warn.TLabel", wraplength=230).grid(
            row=r + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.tc_var = tk.BooleanVar(value=self.ctl.config.temperature.compensation_enabled)
        ttk.Checkbutton(left, text="Temperature compensation", variable=self.tc_var,
                        command=self.on_toggle_tc).grid(row=r + 2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.tc_alpha_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.tc_alpha_var, style="Unit.TLabel").grid(row=r + 3, column=0, columnspan=2, sticky="w")

        sim = ttk.LabelFrame(left, text="Simulation controls", padding=8)
        sim.grid(row=r + 4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.sim_mass_var = tk.DoubleVar(value=0.0)
        self.sim_mass_text = tk.StringVar(value="Tip mass: 0 g")
        ttk.Label(sim, textvariable=self.sim_mass_text).pack(anchor="w")
        self.sim_scale = ttk.Scale(sim, from_=0.0, to=3000.0, orient=tk.HORIZONTAL, variable=self.sim_mass_var,
                                   command=self.on_sim_mass, length=210)
        self.sim_scale.pack(fill=tk.X, pady=4)
        self.btn_tap = ttk.Button(sim, text="Tap the beam", command=self.ctl.sim_tap)
        self.btn_tap.pack(fill=tk.X)
        self._update_alpha_label()

        self.live_fig = Figure(figsize=(9, 7), dpi=100)
        self.live_axes = self.live_fig.subplots(len(LIVE_TRACES), 1, sharex=True)
        self.live_lines = {}
        for ax, (key, label, color, _span) in zip(self.live_axes, LIVE_TRACES):
            (line,) = ax.plot([], [], color=color, linewidth=1.0)
            self.live_lines[key] = line
            ax.set_ylabel(label)
            ax.margins(x=0)
        self.live_axes[-1].set_xlabel("Time [s]")
        self.live_axes[0].set_title("Live signals")
        self.live_fig.subplots_adjust(left=0.10, right=0.98, top=0.95, bottom=0.07, hspace=0.12)
        self.live_canvas = self._make_canvas(right, self.live_fig)

    # ------------------------------------------------------------------ calibration tab
    def _build_calibration_tab(self) -> None:
        left = ttk.Frame(self.tab_cal, padding=10)
        left.pack(side=tk.LEFT, fill=tk.Y)
        right = ttk.Frame(self.tab_cal)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        help_text = ("1. Start acquisition and press Tare with the beam unloaded.\n"
                     "2. Hang a known mass on the tip, enter it below, press Capture.\n"
                     "3. Repeat for 4–6 masses, then Fit and Apply.")
        ttk.Label(left, text=help_text, justify=tk.LEFT, wraplength=330).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(left, text="Tip mass [g]").grid(row=1, column=0, sticky="w", pady=(10, 2))
        self.cal_mass_var = tk.StringVar(value="0")
        ttk.Entry(left, textvariable=self.cal_mass_var, width=10).grid(row=1, column=1, sticky="w", pady=(10, 2))
        ttk.Label(left, text="Average over [s]").grid(row=2, column=0, sticky="w")
        self.cal_secs_var = tk.StringVar(value="2")
        ttk.Spinbox(left, from_=0.5, to=20.0, increment=0.5, textvariable=self.cal_secs_var, width=8).grid(row=2, column=1, sticky="w")
        self.btn_capture = ttk.Button(left, text="Capture point", style="Accent.TButton", command=self.on_capture)
        self.btn_capture.grid(row=1, column=2, rowspan=2, padx=(8, 0), sticky="ns")

        cols = ("idx", "mass", "measured", "std", "theory")
        self.cal_tree = ttk.Treeview(left, columns=cols, show="headings", height=9, selectmode="browse")
        for c, title, w in zip(cols, ("#", "Mass [g]", "Measured [µε]", "± std", "Theory [µε]"), (35, 75, 100, 65, 95)):
            self.cal_tree.heading(c, text=title)
            self.cal_tree.column(c, width=w, anchor="e")
        self.cal_tree.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(10, 4))
        ttk.Button(left, text="Remove selected", command=self.on_cal_remove).grid(row=4, column=0, sticky="w")
        ttk.Button(left, text="Clear all", command=self.on_cal_clear).grid(row=4, column=1, sticky="w")

        fitf = ttk.LabelFrame(left, text="Fit", padding=8)
        fitf.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.cal_order_var = tk.IntVar(value=1)
        ttk.Radiobutton(fitf, text="Linear", variable=self.cal_order_var, value=1).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(fitf, text="Quadratic", variable=self.cal_order_var, value=2).grid(row=0, column=1, sticky="w")
        ttk.Button(fitf, text="Fit", command=self.on_cal_fit).grid(row=0, column=2, padx=(10, 0))
        self.cal_result_var = tk.StringVar(value="No fit yet.")
        ttk.Label(fitf, textvariable=self.cal_result_var, justify=tk.LEFT, wraplength=320).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 6))
        self.btn_cal_apply = ttk.Button(fitf, text="Apply calibration", command=self.on_cal_apply)
        self.btn_cal_apply.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(fitf, text="Disable", command=self.on_cal_disable).grid(row=2, column=2, sticky="e")
        self.cal_state_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.cal_state_var, style="Unit.TLabel", wraplength=330, justify=tk.LEFT).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        left.rowconfigure(3, weight=1)

        self.cal_fig = Figure(figsize=(7, 6), dpi=100)
        self.cal_canvas = self._make_canvas(right, self.cal_fig)
        self._refresh_calibration_view()

    # ------------------------------------------------------------------ analysis tab
    def _build_analysis_tab(self) -> None:
        top = ttk.Frame(self.tab_ana, padding=(10, 8))
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(top, text="Analyse current recording", style="Accent.TButton",
                   command=self.on_analyse_current).pack(side=tk.LEFT)
        ttk.Button(top, text="Open recording…", command=self.on_open_recording).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Save figure…", command=self.on_save_figure).pack(side=tk.LEFT)
        self.btn_use_alpha = ttk.Button(top, text="Use fitted α for compensation", command=self.on_use_alpha)
        self.btn_use_alpha.pack(side=tk.LEFT, padx=6)
        self.ana_source_var = tk.StringVar(value="No data loaded.")
        ttk.Label(top, textvariable=self.ana_source_var).pack(side=tk.LEFT, padx=12)

        body = ttk.Frame(self.tab_ana)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        textf = ttk.Frame(body, padding=(10, 0))
        textf.pack(side=tk.LEFT, fill=tk.Y)
        self.ana_text = tk.Text(textf, width=46, wrap="none", font=("DejaVu Sans Mono", 9), relief="flat",
                                background="#f6f7f5")
        self.ana_text.pack(side=tk.LEFT, fill=tk.Y, expand=True)
        self._set_analysis_text("Record a run on the Live tab, then press 'Analyse current recording',\n"
                                "or open a saved CSV file.\n\nFor the thermal drift α, record the UNLOADED\n"
                                "beam while the temperature changes.")
        figf = ttk.Frame(body)
        figf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.ana_fig = Figure(figsize=(9, 7), dpi=100)
        self.ana_canvas = self._make_canvas(figf, self.ana_fig)

    # ------------------------------------------------------------------ settings tab
    def _build_settings_tab(self) -> None:
        outer = ttk.Frame(self.tab_set, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        self.setting_vars: Dict[Tuple[str, str], tk.Variable] = {}
        grid = ttk.Frame(outer)
        grid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        positions = {"beam": (0, 0), "strain": (0, 1), "pzt": (1, 1), "daq": (0, 2), "temperature": (1, 0)}
        for section, (row, col) in positions.items():
            frame = ttk.LabelFrame(grid, text=SECTION_TITLES[section], padding=8)
            frame.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
            obj = getattr(self.ctl.config, section)
            for i, f in enumerate(fields(obj)):
                value = getattr(obj, f.name)
                ttk.Label(frame, text=SETTING_LABELS.get(f.name, f.name)).grid(row=i, column=0, sticky="w", pady=2)
                key = (section, f.name)
                if isinstance(value, bool):
                    var: tk.Variable = tk.BooleanVar(value=value)
                    ttk.Checkbutton(frame, variable=var).grid(row=i, column=1, sticky="w")
                elif key in CHOICES:
                    var = tk.StringVar(value=str(value))
                    ttk.Combobox(frame, textvariable=var, values=[str(v) for v in CHOICES[key]], state="readonly",
                                 width=14).grid(row=i, column=1, sticky="w")
                else:
                    var = tk.StringVar(value=str(value))
                    ttk.Entry(frame, textvariable=var, width=30 if isinstance(value, str) else 16).grid(row=i, column=1, sticky="w")
                self.setting_vars[key] = var
        for c in range(3):
            grid.columnconfigure(c, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        self.btn_apply_settings = ttk.Button(buttons, text="Apply", style="Accent.TButton", command=self.on_apply_settings)
        self.btn_apply_settings.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Load from file…", command=self.on_load_settings).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Save to file…", command=self.on_save_settings).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Reset to defaults", command=self.on_reset_settings).pack(side=tk.LEFT, padx=6)
        self.beam_info_var = tk.StringVar(value="")
        ttk.Label(buttons, textvariable=self.beam_info_var).pack(side=tk.LEFT, padx=16)
        self._update_beam_info()

    # ================================================================== helpers
    def _schedule(self, name: str, ms: int, func) -> None:
        def wrapper():
            try:
                func()
            except Exception:  # never let a refresh error kill the GUI loop
                log.exception("Error in periodic task %s", name)
            self._jobs[name] = self.root.after(ms, wrapper)
        self._jobs[name] = self.root.after(ms, wrapper)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        log.info(text)

    def _error(self, title: str, exc_or_msg) -> None:
        msg = str(exc_or_msg)
        log.error("%s: %s", title, msg)
        messagebox.showerror(title, msg, parent=self.root)

    @staticmethod
    def _set_state(widget, enabled: bool) -> None:
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _update_controls(self) -> None:
        running = self.ctl.running
        sim = running and self.ctl.simulation
        self._set_state(self.btn_start, not running)
        self._set_state(self.btn_stop, running)
        self._set_state(self.btn_tare, running)
        self._set_state(self.btn_record, running)
        self._set_state(self.btn_save, self.ctl.recorded_samples > 0 and not self.ctl.recording)
        self._set_state(self.rb_hw, not running)
        self._set_state(self.rb_sim, not running)
        self._set_state(self.btn_capture, running and self._capture_job is None)
        self._set_state(self.btn_apply_settings, not running)
        self._set_state(self.sim_scale, sim)
        self._set_state(self.btn_tap, sim)
        self.btn_record.configure(text="■ Stop recording" if self.ctl.recording else "● Record")
        if running:
            self.mode_label.configure(text="SIMULATION" if self.ctl.simulation else "HARDWARE")
        else:
            self.mode_label.configure(text="stopped")

    def _update_alpha_label(self) -> None:
        tc = self.ctl.config.temperature
        self.tc_alpha_var.set(f"α = {tc.alpha_ue_per_c:.3f} µε/°C  (set on Analysis tab)")

    def _update_beam_info(self) -> None:
        try:
            beam = BeamModel(self.ctl.config.beam)
            per_kg = float(beam.strain_from_mass(1000.0)) * 1e6
            self.beam_info_var.set(f"Beam theory: f₁ = {beam.natural_frequency_hz():.1f} Hz, "
                                   f"{per_kg:.2f} µε per kg at the gauge")
        except ValueError as exc:
            self.beam_info_var.set(str(exc))

    def _set_analysis_text(self, text: str) -> None:
        self.ana_text.configure(state="normal")
        self.ana_text.delete("1.0", tk.END)
        self.ana_text.insert("1.0", text)
        self.ana_text.configure(state="disabled")

    # ================================================================== periodic tasks
    def _poll(self) -> None:
        if self.ctl.worker is None and self.ctl.frontend is None:
            return
        result = self.ctl.poll()
        if result.errors:
            self.ctl.stop()
            self._update_controls()
            self._error("Acquisition stopped", result.errors[-1])
        if self.ctl.running or result.new_samples:
            rec = f"  ●REC {self.ctl.recorded_samples} samples" if self.ctl.recording else ""
            self.info_var.set(f"{self.ctl.total_samples} samples{rec}")

    def _refresh_plot(self) -> None:
        if not self.ctl.running or not len(self.ctl.store):
            return
        window = self.ctl.config.daq.plot_window_s
        keys = [k for k, *_ in LIVE_TRACES]
        a = self.ctl.store.arrays(["t_s"] + keys, last_seconds=window)
        t = a["t_s"]
        if not t.size:
            return
        step = max(1, t.size // MAX_PLOT_POINTS)
        for ax, (key, _label, _color, min_span) in zip(self.live_axes, LIVE_TRACES):
            y = a[key]
            self.live_lines[key].set_data(t[::step], y[::step])
            finite = y[np.isfinite(y)]
            if finite.size:
                lo, hi = float(finite.min()), float(finite.max())
                pad = max((hi - lo) * 0.1, min_span / 2)
                mid = 0.5 * (lo + hi)
                half = max((hi - lo) / 2 + pad, min_span / 2)
                ax.set_ylim(mid - half, mid + half)
        self.live_axes[0].set_xlim(max(0.0, t[-1] - window), max(t[-1], window))
        self.live_canvas.draw_idle()

    def _refresh_readouts(self) -> None:
        if not len(self.ctl.store):
            return
        values = self.ctl.latest_values()
        for key, _name, _unit, fmt in READOUTS:
            v = values.get(key, math.nan)
            self.readout_vars[key].set(fmt.format(v) if np.isfinite(v) else "—")
        self.rate_var.set(f"Rate: {self.ctl.sample_rate_hz:.0f} samples/s  |  "
                          f"{'DS18B20 OK' if self.ctl.temperature_available else 'no DS18B20'}")
        warn = []
        if values.get("strain_clip", 0) > 0:
            warn.append("Strain amplifier saturated: reduce load or INA gain.")
        if values.get("pzt_clip", 0) > 0:
            warn.append("PZT charge amp saturated: increase Cf (C26).")
        self.clip_var.set("\n".join(warn))

    # ================================================================== toolbar actions
    def on_start(self) -> None:
        simulate = self.mode_var.get() == "simulation"
        try:
            self.ctl.start(simulate=simulate)
        except HardwareError as exc:
            if messagebox.askyesno("Hardware not available",
                                   f"{exc}\n\nStart in simulation mode instead?", parent=self.root):
                self.mode_var.set("simulation")
                self.on_start()
            return
        except (ValueError, RuntimeError) as exc:
            self._error("Cannot start", exc)
            return
        if simulate:
            self.ctl.set_sim_mass(self.sim_mass_var.get())
        for line in self.live_lines.values():
            line.set_data([], [])
        self._update_controls()
        self.set_status(f"Running ({'simulation' if simulate else 'hardware'}). Press Tare with the beam unloaded.")

    def on_stop(self) -> None:
        self.ctl.stop()
        if self._capture_job is not None:
            self.root.after_cancel(self._capture_job)
            self._capture_job = None
        if self.ctl.recorded_samples:
            self._unsaved = True
        self._update_controls()
        self.set_status("Stopped.")

    def on_tare(self) -> None:
        try:
            self.ctl.tare()
        except RuntimeError as exc:
            self._error("Tare", exc)
            return
        t0 = self.ctl.processor.tare.temp0
        self.set_status("Tared" + (f" at {t0:.2f} °C." if np.isfinite(t0) else "."))

    def on_record(self) -> None:
        if self.ctl.recording:
            n = self.ctl.stop_recording()
            self._unsaved = n > 0
            self.set_status(f"Recording stopped: {n} samples. Use 'Save recording…' or the Analysis tab.")
        else:
            if self._unsaved and not messagebox.askyesno(
                    "Unsaved recording", "The previous recording is not saved. Discard it?", parent=self.root):
                return
            try:
                self.ctl.start_recording()
            except RuntimeError as exc:
                self._error("Record", exc)
                return
            self._unsaved = False
            self.set_status("Recording…")
        self._update_controls()

    def on_save_recording(self) -> None:
        if not self.ctl.recorded_samples:
            self._error("Save recording", "Nothing recorded yet.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save recording", defaultextension=".csv",
            initialfile=f"cantilever_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV file", "*.csv"), ("Excel workbook", "*.xlsx")])
        if not path:
            return
        try:
            data_path, meta_path = self.ctl.save_recording(path)
        except ImportError:
            self._error("Save recording", "Saving .xlsx needs openpyxl: sudo apt install python3-openpyxl\n"
                                          "Save as .csv instead.")
            return
        except (OSError, RuntimeError) as exc:
            self._error("Save recording", exc)
            return
        self._unsaved = False
        self.set_status(f"Saved {data_path.name} (+ {meta_path.name} with all settings).")

    # ================================================================== live tab actions
    def on_toggle_tc(self) -> None:
        enabled = bool(self.tc_var.get())
        if enabled and not np.isfinite(self.ctl.processor.tare.temp0):
            self.set_status("Temperature compensation needs a reference: press Tare.")
        self.ctl.set_temperature_compensation(enabled)
        self.setting_vars[("temperature", "compensation_enabled")].set(enabled)

    def on_sim_mass(self, _value=None) -> None:
        mass = round(float(self.sim_mass_var.get()) / 10.0) * 10.0
        self.sim_mass_text.set(f"Tip mass: {mass:.0f} g")
        self.ctl.set_sim_mass(mass)

    # ================================================================== calibration actions
    def on_capture(self) -> None:
        try:
            mass = float(self.cal_mass_var.get().replace(",", "."))
            secs = float(self.cal_secs_var.get().replace(",", "."))
            if mass < 0 or not 0.2 <= secs <= 60:
                raise ValueError
        except ValueError:
            self._error("Capture", "Enter a mass ≥ 0 g and an averaging time between 0.2 and 60 s.")
            return
        settle = 0.0
        if self.ctl.simulation:  # in simulation, move the virtual mass and let the beam settle
            self.sim_mass_var.set(mass)
            self.on_sim_mass()
            settle = 1.0
        self.set_status(f"Capturing {mass:g} g: averaging {secs:g} s…")
        self._capture_job = self.root.after(int((settle + secs) * 1000), lambda: self._finish_capture(mass, secs))
        self._update_controls()

    def _finish_capture(self, mass: float, secs: float) -> None:
        self._capture_job = None
        try:
            p = self.ctl.capture_calibration_point(mass, secs)
        except RuntimeError as exc:
            self._error("Capture", exc)
        else:
            self.set_status(f"Captured {p.mass_g:g} g: {p.measured_ue:.3f} ± {p.std_ue:.3f} µε "
                            f"(theory {p.theory_ue:.3f} µε).")
        self._update_controls()
        self._refresh_calibration_view()

    def on_cal_remove(self) -> None:
        sel = self.cal_tree.selection()
        if not sel:
            return
        self.ctl.remove_calibration_point(self.cal_tree.index(sel[0]))
        self._last_fit = None
        self._refresh_calibration_view()

    def on_cal_clear(self) -> None:
        if self.ctl.cal_points and messagebox.askyesno("Clear", "Remove all calibration points?", parent=self.root):
            self.ctl.clear_calibration_points()
            self._last_fit = None
            self._refresh_calibration_view()

    def on_cal_fit(self) -> None:
        try:
            self._last_fit = self.ctl.fit_calibration(int(self.cal_order_var.get()))
        except ValueError as exc:
            self._error("Fit", exc)
            return
        f = self._last_fit
        terms = " ".join(f"{c:+.5g}·x^{len(f.coefficients) - 1 - i}" for i, c in enumerate(f.coefficients))
        self.cal_result_var.set(f"true = {terms}\nR² = {f.r2:.5f}   sensitivity = {f.sensitivity:.4f}\n"
                                f"max residual = {max(abs(r) for r in f.residuals_ue):.3f} µε")
        self._refresh_calibration_view()

    def on_cal_apply(self) -> None:
        if self._last_fit is None:
            self._error("Apply calibration", "Fit the calibration points first.")
            return
        self.ctl.apply_calibration(self._last_fit.coefficients)
        self._save_settings_quietly()
        self._refresh_calibration_view()
        self.set_status("Calibration applied to all strain, deflection and load values.")

    def on_cal_disable(self) -> None:
        self.ctl.disable_calibration()
        self._save_settings_quietly()
        self._refresh_calibration_view()
        self.set_status("Calibration disabled.")

    def _refresh_calibration_view(self) -> None:
        for item in self.cal_tree.get_children():
            self.cal_tree.delete(item)
        for i, p in enumerate(self.ctl.cal_points, 1):
            self.cal_tree.insert("", tk.END, values=(i, f"{p.mass_g:g}", f"{p.measured_ue:.3f}",
                                                     f"{p.std_ue:.3f}", f"{p.theory_ue:.3f}"))
        cal = self.ctl.config.calibration
        coeffs = ", ".join(f"{c:.5g}" for c in cal.coefficients)
        self.cal_state_var.set(f"Active calibration: {'ON' if cal.enabled else 'off'}  [{coeffs}]")
        try:
            beam = BeamModel(self.ctl.config.beam)
        except ValueError:
            return
        plot_calibration(self.cal_fig, self.ctl.cal_points, beam,
                         self._last_fit.coefficients if self._last_fit else None)
        self.cal_canvas.draw_idle()

    # ================================================================== analysis actions
    def on_analyse_current(self) -> None:
        df = self.ctl.recording_dataframe()
        if df.empty:
            self._error("Analysis", "No recording yet: press Record on the toolbar, then Stop recording.")
            return
        self._run_analysis(df, f"Current recording ({len(df)} samples)")

    def on_open_recording(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Open recording",
                                          filetypes=[("Recordings", "*.csv *.xlsx"), ("All files", "*.*")])
        if not path:
            return
        try:
            df, _meta = load_recording(path)
        except (OSError, ValueError, ImportError) as exc:
            self._error("Open recording", exc)
            return
        self._run_analysis(df, f"{Path(path).name} ({len(df)} samples)")

    def _run_analysis(self, df, source: str) -> None:
        self._analysis_df = df
        lines = [f"Source: {source}", f"Duration: {df['t_s'].iloc[-1] - df['t_s'].iloc[0]:.2f} s", ""]
        table = summary_table(df)
        if not table.empty:
            lines.append(table.to_string(float_format=lambda v: f"{v:.4g}"))
            lines.append("")
        try:
            spec = spectrum(df, "pzt_mV")
        except ValueError:
            try:
                spec = spectrum(df, "strain_ue")
            except ValueError:
                spec = None
        f1 = BeamModel(self.ctl.config.beam).natural_frequency_hz()
        if spec is not None:
            dev = (spec.peak_hz - f1) / f1 * 100 if np.isfinite(spec.peak_hz) else math.nan
            lines += [f"Natural frequency (from {spec.column}):", f"  measured {spec.peak_hz:8.2f} Hz",
                      f"  theory   {f1:8.2f} Hz   ({dev:+.1f} %)", f"  sample rate {spec.fs:.0f} Hz", ""]
        try:
            self._drift = temperature_drift(df)
            lines += ["Thermal drift (unloaded beam):", f"  α = {self._drift.alpha_ue_per_c:.4f} µε/°C",
                      f"  R² = {self._drift.r2:.4f}  (n = {self._drift.n})"]
            if not self._drift.reliable:
                lines += ["  WARNING: temperature explains little of the", "  strain change. Was the beam loaded?",
                          "  Record the UNLOADED beam for this fit."]
        except ValueError as exc:
            self._drift = None
            lines += ["Thermal drift: " + str(exc)]
        self._set_analysis_text("\n".join(lines))
        plot_analysis(self.ana_fig, df, self.ctl.config, spec, self._drift)
        self.ana_canvas.draw_idle()
        self.ana_source_var.set(source)
        self.nb.select(self.tab_ana)

    def on_use_alpha(self) -> None:
        if self._drift is None:
            self._error("Thermal drift", "Analyse a recording with a temperature change first.")
            return
        alpha = self._drift.alpha_ue_per_c
        if not self._drift.reliable and not messagebox.askyesno(
                "Unreliable drift fit",
                f"R² = {self._drift.r2:.2f}: the strain changes in this recording are not explained by temperature "
                f"(the beam was probably loaded).\n\nUse α = {alpha:.3f} µε/°C anyway?", parent=self.root):
            return
        self.ctl.set_temperature_compensation(True, alpha)
        self.tc_var.set(True)
        self.setting_vars[("temperature", "alpha_ue_per_c")].set(f"{alpha:.5g}")
        self.setting_vars[("temperature", "compensation_enabled")].set(True)
        self._update_alpha_label()
        self._save_settings_quietly()
        self.set_status(f"Temperature compensation ON with α = {alpha:.4f} µε/°C. Press Tare to set the reference.")

    def on_save_figure(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, title="Save figure", defaultextension=".png",
                                            initialfile=f"analysis_{datetime.now():%Y%m%d_%H%M%S}.png",
                                            filetypes=[("PNG image", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")])
        if path:
            try:
                self.ana_fig.savefig(path, dpi=150)
            except (OSError, ValueError) as exc:
                self._error("Save figure", exc)
                return
            self.set_status(f"Figure saved: {Path(path).name}")

    # ================================================================== settings actions
    def _config_from_form(self) -> AppConfig:
        cfg = self.ctl.config.copy()
        for (section, name), var in self.setting_vars.items():
            obj = getattr(cfg, section)
            default = getattr(obj, name)
            raw = var.get()
            try:
                if isinstance(default, bool):
                    value = bool(raw)
                elif isinstance(default, int):
                    value = int(float(str(raw).replace(",", ".")))
                elif isinstance(default, float):
                    value = float(str(raw).replace(",", "."))
                else:
                    value = str(raw).strip()
            except ValueError:
                raise ValueError(f"'{SETTING_LABELS.get(name, name)}' is not a valid number: {raw!r}") from None
            setattr(obj, name, value)
        return cfg

    def _fill_form(self, cfg: AppConfig) -> None:
        for (section, name), var in self.setting_vars.items():
            value = getattr(getattr(cfg, section), name)
            var.set(value if isinstance(value, bool) else str(value))
        self.tc_var.set(cfg.temperature.compensation_enabled)

    def on_apply_settings(self) -> None:
        try:
            cfg = self._config_from_form()
            self.ctl.update_config(cfg)
        except (ValueError, RuntimeError) as exc:
            self._error("Settings", exc)
            return
        self._save_settings_quietly()
        self._update_beam_info()
        self._update_alpha_label()
        self._refresh_calibration_view()
        self.set_status("Settings applied and saved.")

    def on_load_settings(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Load settings",
                                          filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            cfg = AppConfig.load(path)
        except (OSError, ValueError, TypeError) as exc:
            self._error("Load settings", exc)
            return
        self._fill_form(cfg)
        self.set_status(f"Loaded {Path(path).name}. Press Apply to use it.")

    def on_save_settings(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, title="Save settings", defaultextension=".json",
                                            initialfile="cantilever_settings.json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            self._config_from_form().save(path)
        except (OSError, ValueError) as exc:
            self._error("Save settings", exc)
            return
        self.set_status(f"Settings saved to {Path(path).name}.")

    def on_reset_settings(self) -> None:
        if messagebox.askyesno("Reset", "Fill the form with default values? (Press Apply afterwards.)", parent=self.root):
            self._fill_form(AppConfig())

    def _save_settings_quietly(self) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.ctl.config.save(self.settings_path)
        except OSError as exc:
            log.warning("Could not save settings to %s: %s", self.settings_path, exc)

    # ================================================================== shutdown
    def on_close(self) -> None:
        if (self._unsaved or self.ctl.recording) and not messagebox.askyesno(
                "Quit", "The recording has not been saved. Quit anyway?", parent=self.root):
            return
        for job in list(self._jobs.values()) + [self._capture_job]:
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        try:
            self.ctl.shutdown()
        finally:
            self._save_settings_quietly()
            self.root.destroy()


def run_gui(config: Optional[AppConfig] = None, prefer_simulation: bool = False,
            settings_path: Path = DEFAULT_SETTINGS_PATH) -> None:
    root = tk.Tk()
    DAQApp(root, ExperimentController(config), prefer_simulation, settings_path)
    root.mainloop()


if __name__ == "__main__":
    from cantilever_daq.__main__ import main
    sys.exit(main())
