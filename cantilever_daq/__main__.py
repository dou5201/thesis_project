"""Command-line entry point:  python3 -m cantilever_daq [--simulate | --file DATAFILE] [--config FILE]"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import AppConfig

LOG_DIR = Path.home() / ".cache" / "cantilever_daq"


def setup_logging(level: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "cantilever_daq.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    return log_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cantilever_daq",
                                     description="Cantilever beam DAQ: strain gauge + PZT + DS18B20 on Raspberry Pi 5")
    parser.add_argument("--simulate", action="store_true", help="start in simulation mode (no hardware needed)")
    parser.add_argument("--file", metavar="DATAFILE",
                        help="start in File mode, plotting this CSV/XLSX file (or the newest file in this folder) live")
    parser.add_argument("--config", type=Path, help="settings JSON file to load and to save into")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    log_file = setup_logging(args.log_level)
    log = logging.getLogger("cantilever_daq")
    log.info("cantilever_daq %s starting, log file %s", __version__, log_file)

    try:
        from .gui import DEFAULT_SETTINGS_PATH, run_gui
    except ImportError as exc:
        if "tkinter" in str(exc) or "_tkinter" in str(exc):
            print("Tkinter is missing. Install it with:  sudo apt install python3-tk", file=sys.stderr)
            return 2
        raise

    settings_path = args.config or DEFAULT_SETTINGS_PATH
    config = AppConfig()
    if settings_path.exists():
        try:
            config = AppConfig.load(settings_path)
            problems = config.validate()
            if problems:
                log.warning("Settings file has problems, using defaults: %s", "; ".join(problems))
                config = AppConfig()
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Could not read %s (%s); using defaults.", settings_path, exc)
            config = AppConfig()
    elif args.config:
        log.warning("%s does not exist yet; it will be created when you apply settings.", settings_path)

    source = None
    if args.file:
        config.file_source.path = str(Path(args.file).expanduser().resolve())
        source = "file"
    run_gui(config, prefer_simulation=args.simulate, settings_path=settings_path, prefer_source=source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
