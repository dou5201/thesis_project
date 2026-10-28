#!/usr/bin/env python3
"""Start the Cantilever Beam DAQ.

    python run_app.py              hardware if available, otherwise simulation
    python run_app.py --simulate   always simulation (any PC, no hardware needed)

Works from any folder and by double-click (Windows: run_windows.bat).
"""
import importlib
import platform
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REQUIRED = {"numpy": "numpy", "pandas": "pandas", "matplotlib": "matplotlib", "seaborn": "seaborn"}


def check_requirements() -> bool:
    if sys.version_info < (3, 9):
        print(f"Python 3.9 or newer is needed (this is {platform.python_version()}).")
        return False
    missing = []
    for module, package in REQUIRED.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    try:
        importlib.import_module("tkinter")
        tk_ok = True
    except ImportError:
        tk_ok = False
    if not missing and tk_ok:
        return True
    print("Some required Python packages are missing.\n")
    on_pi = platform.system() == "Linux" and platform.machine() in ("aarch64", "armv7l")
    if on_pi:
        apt = {"numpy": "python3-numpy", "pandas": "python3-pandas", "matplotlib": "python3-matplotlib",
               "seaborn": "python3-seaborn"}
        pkgs = [apt[m] for m in missing] + ([] if tk_ok else ["python3-tk"])
        print("Install them with:\n    sudo apt install " + " ".join(pkgs))
    else:
        if missing:
            print("Install them with:\n    " + f'"{sys.executable}" -m pip install ' + " ".join(missing))
        if not tk_ok:
            print("\nTkinter is missing. On Windows/macOS reinstall Python from python.org and keep\n"
                  "'tcl/tk and IDLE' ticked. On Linux: sudo apt install python3-tk")
    return False


if __name__ == "__main__":
    if not check_requirements():
        sys.exit(1)
    args = sys.argv[1:]
    # No SPI hardware on a normal PC: go straight to simulation mode.
    if platform.system() != "Linux" and "--simulate" not in args:
        args.append("--simulate")
    from cantilever_daq.__main__ import main
    sys.exit(main(args))
