"""Data-file front end: live tailing, column/unit detection, folder mode, Excel, and the SD logger."""
import math
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from cantilever_daq.acquisition import ExperimentController
from cantilever_daq.config import AppConfig
from cantilever_daq.file_source import FileFrontEnd, FileSourceError, load_data_file, normalise_header
from cantilever_daq.processing import BeamModel
from cantilever_daq.sd_logger import run_logger


def _config(path, **kw) -> AppConfig:
    cfg = AppConfig()
    cfg.file_source.path = str(path)
    cfg.file_source.poll_interval_s = 0.02
    for k, v in kw.items():
        setattr(cfg.file_source, k, v)
    return cfg


def _drain(fe, rounds=5):
    out = []
    for _ in range(rounds):
        out += fe.read_available()
    return out


class TestFileSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_header_normalisation(self):
        self.assertEqual(normalise_header("Strain [µε]"), "strain_ue")
        self.assertEqual(normalise_header("Temp. (°C)"), "temp_c")
        self.assertEqual(normalise_header(" PZT mV "), "pzt_mv")

    def test_engineering_units_round_trip_and_partial_lines(self):
        p = self.dir / "log.csv"
        p.write_text("time,Strain [ue],PZT [mV],Temperature\n0.0,10,5,21.5\n0.01,20,-5,21.5\n0.02,3")
        fe = FileFrontEnd(_config(p))
        fe.open()
        self.assertEqual(fe.columns.strain_unit, "ue")
        self.assertEqual(fe.columns.pzt_unit, "mV")
        self.assertFalse(fe.raw_voltages)
        rows = _drain(fe, 2)
        self.assertEqual(len(rows), 2)                    # the half-written third line is held back
        with open(p, "a") as fh:
            fh.write("0,0,21.6\n")
        rows += _drain(fe, 2)
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[2].temp_c, 21.6)

        cfg = fe.cfg
        ctl = ExperimentController(cfg)
        out = ctl.processor.process_batch([r.t for r in rows], [r.v_strain for r in rows],
                                          [r.v_pzt for r in rows], [r.temp_c for r in rows])
        np.testing.assert_allclose(out["strain_ue"], [10, 20, 30], rtol=1e-9)
        np.testing.assert_allclose(out["pzt_mV"], [5, -5, 0], atol=1e-9)
        E = cfg.beam.youngs_modulus_gpa * 1e3                # MPa
        np.testing.assert_allclose(out["stress_MPa"], np.array([10, 20, 30]) * 1e-6 * E, rtol=1e-9)
        fe.close()

    def test_semicolon_decimal_comma_and_timestamps(self):
        p = self.dir / "log.txt"
        p.write_text("# logger v1\nTimestamp;v_strain_V;Temp_C\n"
                     "2026-10-28 10:00:00.000;0,001;20,5\n2026-10-28 10:00:00.500;0,002;20,5\n")
        fe = FileFrontEnd(_config(p))
        fe.open()
        rows = _drain(fe, 1)
        self.assertEqual([round(r.t, 3) for r in rows], [0.0, 0.5])
        self.assertAlmostEqual(rows[1].v_strain, 0.002)
        self.assertTrue(math.isnan(rows[0].v_pzt))
        self.assertTrue(fe.raw_voltages and fe.has_temperature)
        fe.close()

    def test_explicit_columns_and_errors(self):
        p = self.dir / "x.csv"
        p.write_text("a,b,c\n1,2,3\n")
        with self.assertRaises(FileSourceError):
            FileFrontEnd(_config(p)).open()               # nothing recognisable
        fe = FileFrontEnd(_config(p, strain_column="1", strain_unit="ue", pzt_column="c", sample_rate_hz=10))
        fe.open()
        self.assertEqual(fe.columns.index["strain"], 1)
        self.assertEqual(fe.columns.index["pzt"], 2)
        fe.close()
        with self.assertRaises(FileSourceError):
            FileFrontEnd(_config(self.dir / "missing.csv")).open()
        with self.assertRaises(FileSourceError):
            FileFrontEnd(_config("")).open()

    def test_start_at_end_skips_old_rows(self):
        p = self.dir / "log.csv"
        p.write_text("t_s,strain_ue\n" + "".join(f"{i * 0.01},{i}\n" for i in range(100)))
        fe = FileFrontEnd(_config(p, start_at_end=True))
        fe.open()
        self.assertEqual(_drain(fe, 1), [])
        with open(p, "a") as fh:
            fh.write("1.0,100\n")
        self.assertEqual(len(_drain(fe, 1)), 1)
        fe.close()

    def test_folder_follows_newest_file_with_continuous_time(self):
        a = self.dir / "log_a.csv"
        a.write_text("t_s,strain_ue\n0,1\n0.1,2\n")
        fe = FileFrontEnd(_config(self.dir))
        fe.open()
        rows = _drain(fe, 1)
        b = self.dir / "log_b.csv"
        b.write_text("t_s,strain_ue\n0,3\n0.1,4\n")
        os.utime(b, (time.time() + 5, time.time() + 5))
        fe._last_scan = 0.0
        rows += _drain(fe, 2)
        self.assertEqual(fe.path, b)
        t = [r.t for r in rows]
        self.assertEqual(len(t), 4)
        self.assertTrue(all(t2 > t1 for t1, t2 in zip(t, t[1:])), t)
        fe.close()

    def test_excel_file(self):
        p = self.dir / "data.xlsx"
        pd.DataFrame({"t_s": [0.0, 0.1, 0.2], "strain_ue": [1.0, 2.0, 3.0], "temp_C": [20.0, 20.1, 20.2]}).to_excel(p, index=False)
        df = load_data_file(p, AppConfig())
        np.testing.assert_allclose(df["strain_ue"], [1, 2, 3], rtol=1e-9)
        self.assertIn("stress_MPa", df.columns)

    def test_controller_live_file_mode_with_logger(self):
        """The SD logger writes (simulated sensors) while the controller follows the same folder."""
        cfg = AppConfig()
        cfg.file_source.path = str(self.dir)
        stop = threading.Event()
        writer = threading.Thread(target=run_logger, args=(cfg, self.dir, True, 3.0, stop))
        writer.start()
        try:
            deadline = time.time() + 3
            while not any(self.dir.iterdir()) and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)
            ctl = ExperimentController(cfg)
            ctl.start(source="file")
            try:
                self.assertEqual(ctl.source, "file")
                self.assertTrue(ctl.temperature_available)
                end = time.time() + 1.5
                while time.time() < end:
                    time.sleep(0.05)
                    ctl.poll()
                self.assertGreater(ctl.total_samples, 200)
                self.assertGreater(ctl.sample_rate_hz, 100)
                ctl.tare()
                values = ctl.latest_values()
                self.assertTrue(np.isfinite(values["strain_ue"]) and np.isfinite(values["temp_C"]))
                self.assertLess(abs(values["strain_ue"]), 2.0)
                ctl.start_recording()
                time.sleep(0.3)
                ctl.poll()
                ctl.stop_recording()
                self.assertEqual(ctl.metadata()["mode"], "file")
            finally:
                ctl.stop()
        finally:
            stop.set()
            writer.join()
        # the logged file is also readable offline, and stress follows Hooke's law
        df = load_data_file(next(self.dir.glob("log_*.csv")), cfg)
        beam = BeamModel(cfg.beam)
        np.testing.assert_allclose(df["stress_MPa"], beam.stress_from_strain(df["strain_ue"] * 1e-6) / 1e6)


if __name__ == "__main__":
    unittest.main()
