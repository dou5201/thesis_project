"""Headless logger: reads the HAT (ADS1256 + DS18B20) and appends the raw values to a CSV file.

Run it on the Raspberry Pi next to the GUI in File mode (the GUI then only reads the file):

    python3 -m cantilever_daq.sd_logger --out ~/data              # new log_YYYYmmdd_HHMMSS.csv in ~/data
    python3 -m cantilever_daq.sd_logger --out ~/data/run1.csv     # this file (appends if it exists)
    python3 -m cantilever_daq.sd_logger --out ~/data --simulate   # simulated sensors, for testing on any PC

Columns: t_s, v_strain_V, v_pzt_V, temp_C (the same raw columns as the GUI's recordings), so the GUI
applies tare, temperature compensation and calibration while it plots. Stop with Ctrl+C.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .acquisition import TemperatureMonitor
from .config import AppConfig
from .hardware import DS18B20, ADS1256FrontEnd, HardwareError, SimulatedFrontEnd

log = logging.getLogger("cantilever_daq.sd_logger")
HEADER = "t_s,v_strain_V,v_pzt_V,temp_C\n"
FLUSH_S = 0.1    # readers see new rows after at most this long
FSYNC_S = 5.0    # rows are safe on the SD card after a power cut after at most this long


def output_path(out: Path) -> Path:
    if out.suffix.lower() == ".csv":
        return out
    return out / f"log_{datetime.now():%Y%m%d_%H%M%S}.csv"


def run_logger(config: AppConfig, out: Path, simulate: bool = False, duration_s: Optional[float] = None,
               stop_event: Optional[threading.Event] = None) -> Path:
    """Log until ``duration_s`` has passed or ``stop_event`` is set. Returns the file written."""
    stop_event = stop_event or threading.Event()
    path = output_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    frontend = SimulatedFrontEnd(config) if simulate else ADS1256FrontEnd(config.daq)
    frontend.open()
    monitor = None
    if config.temperature.enabled:
        if simulate:
            read = frontend.read_temperature
        else:
            sensor = DS18B20(config.temperature.w1_device_glob)
            read = sensor.read_celsius if sensor.available else None
            if read is None:
                log.warning("No DS18B20 found: temp_C stays empty.")
        if read is not None:
            monitor = TemperatureMonitor(read, config.temperature.poll_interval_s)
            monitor.start()

    new_file = not path.exists() or path.stat().st_size == 0
    t_start = time.perf_counter()
    n = 0
    try:
        with open(path, "a", encoding="utf-8", newline="") as fh:
            if new_file:
                fh.write(HEADER)
            last_flush = last_sync = time.perf_counter()
            log.info("Logging to %s (Ctrl+C to stop)", path)
            while not stop_event.is_set():
                s = frontend.read_sample()
                temp = monitor.latest if monitor is not None else float("nan")
                fh.write(f"{s.t:.6f},{_fmt(s.v_strain)},{_fmt(s.v_pzt)},{_fmt(temp, '.4f')}\n")
                n += 1
                now = time.perf_counter()
                if now - last_flush >= FLUSH_S:
                    fh.flush()
                    last_flush = now
                    if now - last_sync >= FSYNC_S:
                        os.fsync(fh.fileno())
                        last_sync = now
                if duration_s is not None and now - t_start >= duration_s:
                    break
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if monitor is not None:
            monitor.stop()
        frontend.close()
    log.info("Stopped: %d samples written to %s", n, path)
    return path


def _fmt(v: float, spec: str = ".7g") -> str:
    return "" if v != v else format(v, spec)  # NaN -> empty field


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cantilever_daq.sd_logger",
                                     description="Log the HAT's raw sensor values to a CSV file (e.g. on the SD card).")
    parser.add_argument("--out", type=Path, default=Path.home() / "cantilever_data",
                        help="folder for a new timestamped file, or a .csv file (default ~/cantilever_data)")
    parser.add_argument("--config", type=Path, help="settings JSON (default: the GUI's settings)")
    parser.add_argument("--simulate", action="store_true", help="simulated sensors (no hardware needed)")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    settings = args.config or Path.home() / ".config" / "cantilever_daq" / "settings.json"
    config = AppConfig.load(settings) if settings.exists() else AppConfig()
    problems = config.validate()
    if problems:
        print("Invalid settings:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 2

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        run_logger(config, args.out.expanduser(), args.simulate, args.duration, stop)
    except HardwareError as exc:
        print(f"Hardware error: {exc}\nUse --simulate to test without the HAT.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
