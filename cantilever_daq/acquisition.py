"""Acquisition threads, data buffers and the experiment controller (no GUI code here)."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import __version__
from .config import AppConfig
from .file_source import FileFrontEnd
from .hardware import (DS18B20, ADS1256FrontEnd, Sample,
                       SimulatedFrontEnd)
from .processing import (PROCESSED_COLUMNS, SignalProcessor, fit_polynomial,
                         window_mean)

log = logging.getLogger(__name__)
NAN = float("nan")


# ====================================================================== temperature thread
class TemperatureMonitor(threading.Thread):
    """Polls a (slow, blocking) temperature source in the background."""

    def __init__(self, read_func: Callable[[], float], interval_s: float):
        super().__init__(name="TemperatureMonitor", daemon=True)
        self._read = read_func
        self._interval = interval_s
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._value = NAN
        self._warned = False

    @property
    def latest(self) -> float:
        with self._lock:
            return self._value

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                value = float(self._read())
            except Exception as exc:  # sensor unplugged, file vanished, ...
                if not self._warned:
                    log.warning("Temperature read failed: %s", exc)
                    self._warned = True
                value = NAN
            with self._lock:
                self._value = value
            self._stop_evt.wait(self._interval)

    def stop(self) -> None:
        self._stop_evt.set()


# ====================================================================== acquisition thread
class AcquisitionWorker(threading.Thread):
    """Reads samples as fast as the front end allows and ships them to the GUI in batches.

    Front ends that implement ``read_available()`` (the data-file reader) deliver many samples per call."""

    def __init__(self, frontend, out_queue: "queue.Queue", temperature: Optional[TemperatureMonitor],
                 batch_period_s: float = 0.05):
        super().__init__(name="AcquisitionWorker", daemon=True)
        self.frontend = frontend
        self.out_queue = out_queue
        self.temperature = temperature
        self.batch_period_s = batch_period_s
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        batch: List[Sample] = []
        last_flush = time.perf_counter()
        read_many = getattr(self.frontend, "read_available", None)
        try:
            while not self._stop_evt.is_set():
                samples = read_many() if read_many is not None else [self.frontend.read_sample()]
                if self.temperature is not None:
                    temp = self.temperature.latest
                    for s in samples:
                        s.temp_c = temp
                batch.extend(samples)
                now = time.perf_counter()
                if now - last_flush >= self.batch_period_s and batch:
                    self.out_queue.put(("data", batch))
                    batch = []
                    last_flush = now
        except Exception as exc:
            log.exception("Acquisition stopped by an error")
            self.out_queue.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            if batch:
                self.out_queue.put(("data", batch))


# ====================================================================== ring buffer
class DataStore:
    """Fixed-length ring buffers (one per column) for live display and short look-backs."""

    def __init__(self, maxlen: int):
        self.maxlen = int(maxlen)
        self.buffers: Dict[str, Deque[float]] = {c: deque(maxlen=self.maxlen) for c in PROCESSED_COLUMNS}

    def clear(self) -> None:
        for d in self.buffers.values():
            d.clear()

    def __len__(self) -> int:
        return len(self.buffers["t_s"])

    def append(self, batch: Dict[str, np.ndarray]) -> None:
        for c in PROCESSED_COLUMNS:
            self.buffers[c].extend(np.asarray(batch[c], dtype=float).tolist())

    def arrays(self, columns: Optional[Sequence[str]] = None, last_seconds: Optional[float] = None) -> Dict[str, np.ndarray]:
        columns = list(columns or PROCESSED_COLUMNS)
        n = len(self)
        out = {c: np.fromiter(self.buffers[c], dtype=float, count=n) for c in set(columns) | {"t_s"}}
        if last_seconds is not None and n:
            mask = out["t_s"] >= out["t_s"][-1] - last_seconds
            out = {c: v[mask] for c, v in out.items()}
        return {c: out[c] for c in columns}

    def reprocess(self, processor: SignalProcessor) -> None:
        """Recompute engineering values after tare / calibration changes."""
        if not len(self):
            return
        raw = self.arrays(["t_s", "v_strain_V", "v_pzt_V", "temp_C"])
        batch = processor.process_batch(raw["t_s"], raw["v_strain_V"], raw["v_pzt_V"], raw["temp_C"])
        self.clear()
        self.append(batch)


# ====================================================================== calibration records
@dataclass
class CalibrationPoint:
    mass_g: float
    measured_ue: float   # temperature-compensated, uncalibrated
    std_ue: float
    theory_ue: float
    n_samples: int


@dataclass
class CalibrationResult:
    order: int
    coefficients: List[float]
    r2: float
    residuals_ue: List[float]
    sensitivity: float   # d(true)/d(measured) at 0


@dataclass
class PollResult:
    new_samples: int
    errors: List[str]


# ====================================================================== controller
class ExperimentController:
    """Owns the hardware, the threads and the data. The GUI only calls these methods."""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        self.processor = SignalProcessor(self.config)
        self.queue: "queue.Queue" = queue.Queue()
        self.store = DataStore(self._buffer_len())
        self.frontend = None
        self.source = "hardware"   # "hardware", "simulation" or "file"
        self.worker: Optional[AcquisitionWorker] = None
        self.temp_monitor: Optional[TemperatureMonitor] = None
        self.temperature_available = False
        self.recording = False
        self._rec_chunks: List[Dict[str, np.ndarray]] = []
        self.recording_started: Optional[datetime] = None
        self.run_started: Optional[datetime] = None
        self.cal_points: List[CalibrationPoint] = []
        self.sample_rate_hz = 0.0
        self.total_samples = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ properties
    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    @property
    def simulation(self) -> bool:
        return bool(getattr(self.frontend, "is_simulation", False))

    def _buffer_len(self) -> int:
        rate = self.config.daq.data_rate_sps
        return int(min(max(self.config.daq.plot_window_s, 30.0) * rate * 1.2, 300_000))

    # ------------------------------------------------------------ run control
    def start(self, simulate: bool = False, seed: Optional[int] = None, realtime: bool = True,
              source: Optional[str] = None) -> None:
        """Start acquiring from ``source`` ("hardware", "simulation" or "file"; default from ``simulate``)."""
        source = source or ("simulation" if simulate else "hardware")
        simulate = source == "simulation"
        if self.running:
            raise RuntimeError("Acquisition is already running.")
        problems = self.config.validate()
        if problems:
            raise ValueError("Invalid settings:\n- " + "\n- ".join(problems))
        self.processor = SignalProcessor(self.config)
        self.store = DataStore(self._buffer_len())
        self.queue = queue.Queue()
        if simulate:
            frontend = SimulatedFrontEnd(self.config, seed=seed, realtime=realtime)
        elif source == "file":
            frontend = FileFrontEnd(self.config)
        else:
            frontend = ADS1256FrontEnd(self.config.daq)
        frontend.open()  # raises HardwareError with a helpful message
        self.frontend = frontend
        self.source = source
        self.processor.detect_clipping = getattr(frontend, "raw_voltages", True)

        self.temp_monitor = None
        self.temperature_available = False
        if source == "file" and frontend.has_temperature:
            self.temperature_available = True    # the temperature comes from the file
        elif self.config.temperature.enabled:
            if simulate:
                read = frontend.read_temperature
                self.temperature_available = True
            else:
                sensor = DS18B20(self.config.temperature.w1_device_glob)
                self.temperature_available = sensor.available
                read = sensor.read_celsius
                if not sensor.available:
                    log.warning("DS18B20 not found under %s", self.config.temperature.w1_device_glob)
            if self.temperature_available:
                self.temp_monitor = TemperatureMonitor(read, self.config.temperature.poll_interval_s)
                self.temp_monitor.start()

        self.worker = AcquisitionWorker(frontend, self.queue, self.temp_monitor)
        self.worker.start()
        self.run_started = datetime.now()
        self.total_samples = 0
        self.sample_rate_hz = 0.0
        self.last_error = None
        log.info("Acquisition started (%s)", source)

    def stop(self) -> None:
        if self.recording:
            self.stop_recording()
        if self.worker is not None:
            self.worker.stop()
            self.worker.join(timeout=3.0)
            self.worker = None
        if self.temp_monitor is not None:
            self.temp_monitor.stop()
            self.temp_monitor = None
        if self.frontend is not None:
            try:
                self.frontend.close()
            except Exception:
                log.exception("Error while closing the front end")
        self.poll()  # keep the last batch
        log.info("Acquisition stopped")

    def shutdown(self) -> None:
        try:
            self.stop()
        finally:
            self.frontend = None

    def poll(self, max_messages: int = 200) -> PollResult:
        """Drain the worker queue (call regularly from the GUI thread)."""
        errors: List[str] = []
        new = 0
        for _ in range(max_messages):
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                errors.append(payload)
                self.last_error = payload
                continue
            if not payload:
                continue
            t = np.fromiter((s.t for s in payload), float, len(payload))
            vs = np.fromiter((s.v_strain for s in payload), float, len(payload))
            vp = np.fromiter((s.v_pzt for s in payload), float, len(payload))
            tc = np.fromiter((s.temp_c for s in payload), float, len(payload))
            batch = self.processor.process_batch(t, vs, vp, tc)
            self.store.append(batch)
            if self.recording:
                self._rec_chunks.append(batch)
            new += len(payload)
        if new:
            self.total_samples += new
            t = self.store.arrays(["t_s"], last_seconds=1.0)["t_s"]
            if t.size > 2 and t[-1] > t[0]:
                self.sample_rate_hz = (t.size - 1) / (t[-1] - t[0])
        if errors and self.worker is not None and not self.worker.is_alive():
            self.worker = None
        return PollResult(new, errors)

    # ------------------------------------------------------------ live values
    def latest_values(self, window_s: float = 0.25) -> Dict[str, float]:
        cols = ("strain_ue", "deflection_mm", "load_g", "stress_MPa", "pzt_mV", "pzt_charge_nC", "temp_C",
                "strain_clip", "pzt_clip")
        a = self.store.arrays(cols, last_seconds=window_s)
        out = {c: window_mean(a[c]) for c in cols}
        out["strain_clip"] = float(np.nanmax(a["strain_clip"])) if a["strain_clip"].size else 0.0
        out["pzt_clip"] = float(np.nanmax(a["pzt_clip"])) if a["pzt_clip"].size else 0.0
        return out

    def tare(self, window_s: float = 0.5) -> None:
        """Zero strain and PZT using the last `window_s` seconds; store the reference temperature."""
        a = self.store.arrays(["bridge_ratio", "v_pzt_V", "temp_C"], last_seconds=window_s)
        if not a["bridge_ratio"].size:
            raise RuntimeError("No data yet: start the acquisition first.")
        temp0 = window_mean(a["temp_C"])
        if self.temp_monitor is not None and not np.isfinite(temp0):
            temp0 = self.temp_monitor.latest
        self.processor.set_tare(window_mean(a["bridge_ratio"], 0.0), window_mean(a["v_pzt_V"], 0.0), temp0)
        self.store.reprocess(self.processor)
        log.info("Tare: ratio0=%.3e, v_pzt0=%.4f V, T0=%.2f degC", self.processor.tare.ratio0,
                 self.processor.tare.v_pzt0, temp0)

    def set_sim_mass(self, mass_g: float) -> None:
        if isinstance(self.frontend, SimulatedFrontEnd):
            self.frontend.set_mass(mass_g)

    def sim_tap(self) -> None:
        if isinstance(self.frontend, SimulatedFrontEnd):
            self.frontend.tap()

    # ------------------------------------------------------------ recording
    def start_recording(self) -> None:
        if not self.running:
            raise RuntimeError("Start the acquisition before recording.")
        self._rec_chunks = []
        self.recording = True
        self.recording_started = datetime.now()

    def stop_recording(self) -> int:
        self.recording = False
        return self.recorded_samples

    @property
    def recorded_samples(self) -> int:
        return int(sum(len(c["t_s"]) for c in self._rec_chunks))

    def recording_dataframe(self) -> pd.DataFrame:
        if not self._rec_chunks:
            return pd.DataFrame(columns=list(PROCESSED_COLUMNS))
        data = {c: np.concatenate([chunk[c] for chunk in self._rec_chunks]) for c in PROCESSED_COLUMNS}
        df = pd.DataFrame(data)
        df["t_s"] = df["t_s"] - df["t_s"].iloc[0]
        return df

    def metadata(self) -> Dict:
        return {
            "software": f"cantilever_daq {__version__}",
            "created": datetime.now().isoformat(timespec="seconds"),
            "recording_started": self.recording_started.isoformat(timespec="seconds") if self.recording_started else None,
            "mode": self.source,
            "data_file": str(self.frontend.path) if self.source == "file" and self.frontend is not None else None,
            "samples": self.recorded_samples,
            "tare": {"ratio0": self.processor.tare.ratio0, "v_pzt0_V": self.processor.tare.v_pzt0,
                     "temp0_C": self.processor.tare.temp0},
            "config": self.config.to_dict(),
        }

    def save_recording(self, path) -> Tuple[Path, Path]:
        """Save the recording as CSV (or .xlsx) plus a JSON side-car with all settings."""
        df = self.recording_dataframe()
        if df.empty:
            raise RuntimeError("Nothing recorded yet.")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".xlsx":
            df.to_excel(path, index=False, sheet_name="data")  # needs openpyxl
        else:
            df.to_csv(path, index=False, float_format="%.7g")
        meta_path = path.with_suffix(".json")
        meta_path.write_text(json.dumps(self.metadata(), indent=2, default=str), encoding="utf-8")
        log.info("Saved %d samples to %s", len(df), path)
        return path, meta_path

    # ------------------------------------------------------------ calibration
    def capture_calibration_point(self, mass_g: float, seconds: float = 2.0) -> CalibrationPoint:
        a = self.store.arrays(["strain_tc_ue"], last_seconds=seconds)["strain_tc_ue"]
        a = a[np.isfinite(a)]
        if a.size < 10:
            raise RuntimeError("Not enough strain data: is the acquisition running with the strain channel on?")
        theory = float(self.processor.beam.strain_from_mass(mass_g)) * 1e6
        point = CalibrationPoint(float(mass_g), float(a.mean()), float(a.std()), theory, int(a.size))
        self.cal_points.append(point)
        return point

    def remove_calibration_point(self, index: int) -> None:
        del self.cal_points[index]

    def clear_calibration_points(self) -> None:
        self.cal_points.clear()

    def fit_calibration(self, order: int = 1) -> CalibrationResult:
        x = [p.measured_ue for p in self.cal_points]
        y = [p.theory_ue for p in self.cal_points]
        coeffs, r2 = fit_polynomial(x, y, order)
        resid = np.asarray(y) - np.polyval(coeffs, x)
        sens = float(np.polyval(np.polyder(coeffs), 0.0))
        return CalibrationResult(order, [float(c) for c in coeffs], r2, [float(r) for r in resid], sens)

    def apply_calibration(self, coefficients: Sequence[float]) -> None:
        self.config.calibration.coefficients = [float(c) for c in coefficients]
        self.config.calibration.enabled = True
        self.store.reprocess(self.processor)

    def disable_calibration(self) -> None:
        self.config.calibration.enabled = False
        self.store.reprocess(self.processor)

    # ------------------------------------------------------------ settings
    def set_temperature_compensation(self, enabled: bool, alpha_ue_per_c: Optional[float] = None) -> None:
        if alpha_ue_per_c is not None:
            self.config.temperature.alpha_ue_per_c = float(alpha_ue_per_c)
        self.config.temperature.compensation_enabled = bool(enabled)
        self.store.reprocess(self.processor)

    def update_config(self, new_config: AppConfig) -> None:
        if self.running:
            raise RuntimeError("Stop the acquisition before changing settings.")
        problems = new_config.validate()
        if problems:
            raise ValueError("Invalid settings:\n- " + "\n- ".join(problems))
        self.config = new_config
        self.processor = SignalProcessor(new_config)
        self.store = DataStore(self._buffer_len())
