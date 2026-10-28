"""Data-file front end: follows a CSV / TXT / XLSX file (or a folder) that another program writes.

Typical use on the Raspberry Pi: a logger (for example ``python3 -m cantilever_daq.sd_logger``, or
your own program) reads the sensors and appends rows to a file on the SD card; this front end reads
the new rows as they arrive, so the GUI plots them in real time.

Columns are found by their header names (see ``ALIASES``), or by the names / numbers set in the
Settings tab. Strain may be given as the raw bridge voltage [V] or as strain [µε]; the PZT signal as
voltage [V] or [mV]. Engineering values are converted back to the equivalent ADC voltage, so tare,
temperature compensation and calibration work exactly as with the hardware.

Besides ``open()`` and ``close()`` this front end implements ``read_available()``, which returns all
new samples (possibly none) instead of one sample at a time.
"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import AppConfig
from .hardware import HardwareError, Sample
from .processing import strain_to_ratio

log = logging.getLogger(__name__)
NAN = float("nan")

TEXT_SUFFIXES = (".csv", ".txt", ".tsv", ".dat", ".log")
EXCEL_SUFFIXES = (".xlsx", ".xlsm")
DATA_SUFFIXES = TEXT_SUFFIXES + EXCEL_SUFFIXES
UNIT_CHOICES = {"strain": ("auto", "V", "ue"), "pzt": ("auto", "V", "mV")}

# Normalised header names (see normalise_header), most specific first.
ALIASES: Dict[str, Tuple[str, ...]] = {
    "time": ("t_s", "time_s", "t", "time", "timestamp", "datetime", "date_time", "elapsed_s", "seconds"),
    "strain": ("v_strain_v", "v_strain", "strain_v", "strain_ue", "strain", "microstrain", "strain_raw_ue"),
    "pzt": ("v_pzt_v", "v_pzt", "pzt_v", "pzt_mv", "pzt", "piezo_v", "piezo_mv", "piezo"),
    "temp": ("temp_c", "temperature_c", "temperature", "temp", "t_c"),
}
MAX_ROWS_PER_READ = 20_000
FOLDER_SCAN_S = 1.0


class FileSourceError(HardwareError):
    """The data file is missing, unreadable or has no usable columns."""


def normalise_header(name: str) -> str:
    """'Strain [µε]' -> 'strain_ue', 'Temp. (°C)' -> 'temp_c', 'PZT mV' -> 'pzt_mv'."""
    s = str(name).strip().lower()
    s = s.replace("µ", "u").replace("μ", "u").replace("ε", "e").replace("°", "")
    s = re.sub(r"[\[\]()]", " ", s)
    s = re.sub(r"[^0-9a-z]+", "_", s)
    return s.strip("_")


def _unit_from_header(kind: str, header: str) -> str:
    """Guess the unit from the column name. Without a unit, strain is µε and PZT is volts."""
    h = normalise_header(header)
    if kind == "strain":
        return "V" if (h.endswith("_v") or h.startswith("v_")) else "ue"
    return "mV" if h.endswith("_mv") else "V"


def _to_float(text: str, decimal_comma: bool) -> float:
    text = text.strip().strip('"')
    if decimal_comma:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return NAN


def _to_seconds(text: str, decimal_comma: bool) -> float:
    """Numeric time [s] or a date/time stamp (returned as POSIX seconds)."""
    value = _to_float(text, decimal_comma)
    if math.isfinite(value):
        return value
    text = text.strip().strip('"')
    if not text:
        return NAN
    try:
        return datetime.fromisoformat(text.replace("/", "-")).timestamp()
    except ValueError:
        return NAN


def _detect_delimiter(header_line: str) -> Optional[str]:
    counts = {d: header_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else None  # None = split on whitespace


def newest_data_file(folder: Path) -> Optional[Path]:
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in DATA_SUFFIXES]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


# ====================================================================== column mapping
class ColumnMap:
    """Which column holds time / strain / PZT / temperature, and in which unit."""

    def __init__(self, headers: Sequence[str], config: AppConfig):
        fs = config.file_source
        self.headers = list(headers)
        norm = [normalise_header(h) for h in self.headers]
        wanted = {"time": fs.time_column, "strain": fs.strain_column, "pzt": fs.pzt_column, "temp": fs.temp_column}
        self.index: Dict[str, Optional[int]] = {}
        for kind, choice in wanted.items():
            self.index[kind] = self._find(kind, choice.strip(), norm)
        self.strain_unit = fs.strain_unit
        self.pzt_unit = fs.pzt_unit
        if self.strain_unit == "auto" and self.index["strain"] is not None:
            self.strain_unit = _unit_from_header("strain", self.headers[self.index["strain"]])
        if self.pzt_unit == "auto" and self.index["pzt"] is not None:
            self.pzt_unit = _unit_from_header("pzt", self.headers[self.index["pzt"]])
        if self.index["strain"] is None and self.index["pzt"] is None:
            raise FileSourceError(
                "No strain or PZT column found. Columns in the file: " + ", ".join(self.headers) +
                ".\nName them e.g. strain_ue / v_strain_V / pzt_mV / temp_C, or enter the column names "
                "(or numbers, first = 0) in Settings → Data file.")

    def _find(self, kind: str, choice: str, norm: List[str]) -> Optional[int]:
        if choice.lower() in ("", "none", "-"):
            return None
        if choice.lower() != "auto":
            if choice.isdigit():
                i = int(choice)
                if i >= len(self.headers):
                    raise FileSourceError(f"{kind} column {i} does not exist (the file has {len(self.headers)}).")
                return i
            if choice in self.headers:
                return self.headers.index(choice)
            if normalise_header(choice) in norm:
                return norm.index(normalise_header(choice))
            raise FileSourceError(f"{kind} column '{choice}' not found. Columns: " + ", ".join(self.headers))
        for alias in ALIASES[kind]:
            if alias in norm:
                return norm.index(alias)
        return None

    @property
    def raw_voltages(self) -> bool:
        """True if the file holds raw ADC voltages (so amplifier clipping can be detected)."""
        return (self.index["strain"] is None or self.strain_unit == "V") and \
               (self.index["pzt"] is None or self.pzt_unit == "V")

    def describe(self) -> str:
        parts = []
        for kind, unit in (("time", "s"), ("strain", self.strain_unit), ("pzt", self.pzt_unit), ("temp", "°C")):
            i = self.index[kind]
            if i is not None:
                parts.append(f"{kind}='{self.headers[i]}' [{unit}]")
        return ", ".join(parts)


# ====================================================================== front end
class FileFrontEnd:
    """Follows a growing data file. ``config.file_source.path`` may be a file or a folder; for a
    folder the newest data file is used, and the reader switches when the logger starts a new file."""

    is_simulation = False
    source_name = "file"

    def __init__(self, config: AppConfig):
        self.cfg = config
        self.fs = config.file_source
        self.path: Optional[Path] = None
        self.columns: Optional[ColumnMap] = None
        self._folder: Optional[Path] = None
        self._fh = None
        self._offset = 0
        self._partial = ""
        self._delim: Optional[str] = None
        self._decimal_comma = False
        self._excel_rows = 0
        self._excel_mtime = 0.0
        self._last_scan = 0.0
        self._t_offset = 0.0
        self._t_first: Optional[float] = None
        self._last_t = -math.inf
        self._dt = 0.0
        self._row_index = 0
        self._open = False

    # ------------------------------------------------------------ properties
    @property
    def raw_voltages(self) -> bool:
        return self.columns.raw_voltages if self.columns is not None else True

    @property
    def has_temperature(self) -> bool:
        return self.columns is not None and self.columns.index["temp"] is not None

    # ------------------------------------------------------------ open / close
    def open(self) -> None:
        raw = self.fs.path.strip()
        if not raw:
            raise FileSourceError("No data file selected: press 'Choose file…' or set it in Settings → Data file.")
        p = Path(raw).expanduser()
        if p.is_dir():
            self._folder = p
            newest = newest_data_file(p)
            if newest is None:
                raise FileSourceError(f"No data file ({', '.join(DATA_SUFFIXES)}) in folder {p} yet.")
            p = newest
        elif not p.exists():
            raise FileSourceError(f"Data file not found: {p}")
        if p.suffix.lower() not in DATA_SUFFIXES:
            raise FileSourceError(f"Unsupported file type '{p.suffix}'. Use one of: {', '.join(DATA_SUFFIXES)}")
        self._open_file(p, skip_existing=self.fs.start_at_end)
        self._open = True

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self._open = False

    def _open_file(self, path: Path, skip_existing: bool) -> None:
        self.close()
        self.path = path
        self._partial = ""
        self._excel_rows = 0
        self._excel_mtime = 0.0
        if path.suffix.lower() in EXCEL_SUFFIXES:
            headers = self._read_excel_headers(path)
            self.columns = ColumnMap(headers, self.cfg)
            if skip_existing:
                self._excel_rows = len(self._read_excel(path))
        else:
            try:
                self._fh = open(path, "rb")
            except OSError as exc:
                raise FileSourceError(f"Cannot open {path}: {exc}") from exc
            try:
                self.columns = ColumnMap(self._read_text_header(), self.cfg)
            except FileSourceError:
                self.close()
                raise
            if skip_existing:
                self._offset = self._fh.seek(0, 2)
        log.info("Reading %s: %s", path, self.columns.describe())

    # ------------------------------------------------------------ text files
    def _read_text_header(self) -> List[str]:
        """Read up to the header line (skipping blank and '#' comment lines)."""
        deadline = time.monotonic() + 2.0
        while True:
            pos = self._fh.tell()
            raw = self._fh.readline()
            if not raw.endswith(b"\n"):  # header not completely written yet
                if time.monotonic() > deadline:
                    raise FileSourceError(f"{self.path.name} has no header line yet.")
                self._fh.seek(pos)
                time.sleep(0.1)
                continue
            line = raw.decode("utf-8", errors="replace").lstrip("\ufeff").rstrip("\r\n")
            if line.strip() and not line.lstrip().startswith("#"):
                break
        self._delim = _detect_delimiter(line)
        self._decimal_comma = self._delim not in (",", None)
        headers = [h.strip().strip('"') for h in (line.split(self._delim) if self._delim else line.split())]
        if all(math.isfinite(_to_float(h, self._decimal_comma)) for h in headers if h):
            raise FileSourceError(f"{self.path.name} has no header line. Add one, e.g. 't_s,strain_ue,pzt_mV,temp_C'.")
        self._offset = self._fh.tell()
        return headers

    def _new_text_lines(self, final: bool = False) -> List[str]:
        """Complete new lines since the last call (``final``: also the unterminated last line)."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:  # file was truncated or replaced: start again
            log.info("%s became shorter; reading it again from the top.", self.path.name)
            self._open_file(self.path, skip_existing=False)
        self._fh.seek(self._offset)
        data = self._fh.read(4_000_000)
        self._offset += len(data)
        text = self._partial + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._partial = "" if final else lines.pop()  # incomplete last line: wait for the rest
        return lines

    def _parse_text(self, lines: List[str]) -> List[Sample]:
        c = self.columns.index
        out = []
        for line in lines:
            line = line.rstrip("\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            f = line.split(self._delim) if self._delim else line.split()
            get = lambda k: f[c[k]] if c[k] is not None and c[k] < len(f) else ""  # noqa: E731
            if c["strain"] is not None and c["strain"] < len(f) and get("strain").strip() == self.columns.headers[c["strain"]]:
                continue  # repeated header (logger restarted and appended)
            out.append(self._sample(_to_seconds(get("time"), self._decimal_comma) if c["time"] is not None else NAN,
                                    _to_float(get("strain"), self._decimal_comma),
                                    _to_float(get("pzt"), self._decimal_comma),
                                    _to_float(get("temp"), self._decimal_comma)))
        return out

    # ------------------------------------------------------------ excel files
    @staticmethod
    def _read_excel(path: Path):
        import pandas as pd
        try:
            return pd.read_excel(path)
        except ImportError as exc:
            raise FileSourceError("Reading .xlsx needs openpyxl: sudo apt install python3-openpyxl") from exc

    def _read_excel_headers(self, path: Path) -> List[str]:
        try:
            return [str(h) for h in self._read_excel(path).columns]
        except FileSourceError:
            raise
        except Exception as exc:
            raise FileSourceError(f"Cannot read {path.name}: {exc}") from exc

    def _new_excel_samples(self) -> List[Sample]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return []
        if mtime == self._excel_mtime:
            return []
        try:
            df = self._read_excel(self.path)
        except FileSourceError:
            raise
        except Exception:  # the logger is rewriting the workbook right now: try again later
            return []
        self._excel_mtime = mtime
        if len(df) < self._excel_rows:
            self._excel_rows = 0
        new = df.iloc[self._excel_rows:self._excel_rows + MAX_ROWS_PER_READ]
        if len(new) < len(df) - self._excel_rows:
            self._excel_mtime = 0.0  # more rows waiting: read again next time
        self._excel_rows += len(new)
        c = self.columns.index

        def col(kind):
            if c[kind] is None:
                return [NAN] * len(new)
            return new.iloc[:, c[kind]].tolist()

        out = []
        for t, s, p, temp in zip(col("time"), col("strain"), col("pzt"), col("temp")):
            if hasattr(t, "timestamp"):
                t = t.timestamp()
            out.append(self._sample(_cell(t), _cell(s), _cell(p), _cell(temp)))
        return out

    # ------------------------------------------------------------ conversion
    def _sample(self, t_file: float, strain: float, pzt: float, temp: float) -> Sample:
        if not math.isfinite(t_file):
            t_file = self._row_index / self.fs.sample_rate_hz
        self._row_index += 1
        if self._t_first is None:
            self._t_first = t_file
        t = t_file - self._t_first + self._t_offset
        if t <= self._last_t:  # time went backwards (new file, logger restarted): keep the time axis going
            self._t_offset += self._last_t - t + (self._dt or 1.0 / self.fs.sample_rate_hz)
            t = t_file - self._t_first + self._t_offset
        if math.isfinite(self._last_t):
            self._dt = t - self._last_t
        self._last_t = t

        s, p = self.cfg.strain, self.cfg.pzt
        if self.columns.index["strain"] is None:
            v_strain = NAN
        elif self.columns.strain_unit == "ue":
            ratio = float(strain_to_ratio(s.polarity * strain * 1e-6, s.gauge_factor, s.bridge_mode))
            v_strain = ratio * s.ina_gain * s.excitation_v
        else:
            v_strain = strain
        if self.columns.index["pzt"] is None:
            v_pzt = NAN
        elif self.columns.pzt_unit == "mV":
            v_pzt = pzt / 1000.0 * p.polarity
        else:
            v_pzt = pzt
        return Sample(t=t, v_strain=v_strain, v_pzt=v_pzt, temp_c=temp)

    # ------------------------------------------------------------ public
    def read_available(self) -> List[Sample]:
        """All rows written since the last call. Waits one poll interval if there is nothing new."""
        if not self._open:
            raise FileSourceError("Data file is not open.")
        samples = self._check_folder()
        samples += self._read_new()
        if not samples:
            time.sleep(self.fs.poll_interval_s)
        return samples

    def read_all(self) -> List[Sample]:
        """Everything that is in the file now (used for offline loading)."""
        rows: List[Sample] = []
        while True:
            chunk = self._read_new(final=True)
            if not chunk:
                return rows
            rows.extend(chunk)

    def _read_new(self, final: bool = False) -> List[Sample]:
        if self._fh is not None:
            return self._parse_text(self._new_text_lines(final))
        return self._new_excel_samples()

    def _check_folder(self) -> List[Sample]:
        """In folder mode, switch to a newer file once the logger starts one."""
        if self._folder is None or time.monotonic() - self._last_scan < FOLDER_SCAN_S:
            return []
        self._last_scan = time.monotonic()
        try:
            newest = newest_data_file(self._folder)
            if newest is None or newest == self.path or newest.stat().st_mtime <= self.path.stat().st_mtime:
                return []
        except OSError:
            return []
        leftover = self._read_new(final=True)  # finish the old file first
        log.info("New data file in %s: %s", self._folder, newest.name)
        self._t_first = None
        self._row_index = 0
        if math.isfinite(self._last_t):
            self._t_offset = self._last_t + (self._dt or 1.0 / self.fs.sample_rate_hz)
        self._open_file(newest, skip_existing=False)
        self._open = True
        return leftover


def _cell(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return NAN
    return f if np.isfinite(f) else NAN


def load_data_file(path, config: AppConfig):
    """Read a whole data file (any supported layout) and return the processed DataFrame."""
    import pandas as pd
    from .processing import SignalProcessor

    cfg = config.copy()
    cfg.file_source.path = str(path)
    cfg.file_source.start_at_end = False
    fe = FileFrontEnd(cfg)
    fe.open()
    try:
        rows = fe.read_all()
    finally:
        fe.close()
    if not rows:
        raise ValueError(f"{Path(path).name} has no data rows.")
    proc = SignalProcessor(cfg)
    proc.detect_clipping = fe.raw_voltages
    out = proc.process_batch([r.t for r in rows], [r.v_strain for r in rows], [r.v_pzt for r in rows],
                             [r.temp_c for r in rows])
    df = pd.DataFrame(out)
    df["t_s"] = df["t_s"] - df["t_s"].iloc[0]
    return df
