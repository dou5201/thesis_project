"""Headless tests for configuration, processing, simulator and controller.
Run:  python -m unittest discover -s tests -v
"""
import math
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from cantilever_daq.acquisition import DataStore, ExperimentController
from cantilever_daq.analysis import load_recording, spectrum, summary_table, temperature_drift
from cantilever_daq.config import AppConfig
from cantilever_daq.hardware import DS18B20, SimulatedFrontEnd
from cantilever_daq.processing import (BeamModel, SignalProcessor, amplitude_spectrum, dominant_frequency,
                                       fit_polynomial, ratio_to_strain, strain_to_ratio)


class TestConfig(unittest.TestCase):
    def test_defaults_valid(self):
        self.assertEqual(AppConfig().validate(), [])

    def test_roundtrip_json(self):
        cfg = AppConfig()
        cfg.beam.length_mm = 420.0
        cfg.strain.bridge_mode = "quarter"
        cfg.calibration.coefficients = [1.02, -0.3]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            cfg.save(p)
            back = AppConfig.load(p)
        self.assertEqual(back.to_dict(), cfg.to_dict())

    def test_invalid_values_reported(self):
        cfg = AppConfig()
        cfg.beam.gauge_pos_mm = 600
        cfg.daq.data_rate_sps = 1234
        cfg.strain.polarity = 0
        errors = cfg.validate()
        self.assertEqual(len(errors), 3, errors)

    def test_coercion_from_strings(self):
        cfg = AppConfig.from_dict({"daq": {"data_rate_sps": "1000"}, "temperature": {"enabled": "false"}})
        self.assertEqual(cfg.daq.data_rate_sps, 1000)
        self.assertFalse(cfg.temperature.enabled)


class TestProcessing(unittest.TestCase):
    def test_bridge_formulas_roundtrip(self):
        eps = np.array([-500e-6, -10e-6, 0.0, 10e-6, 500e-6])
        for mode in ("quarter", "half", "full"):
            r = strain_to_ratio(eps, 110.0, mode)
            np.testing.assert_allclose(ratio_to_strain(r, 110.0, mode), eps, rtol=1e-12, atol=1e-15)

    def test_half_bridge_value(self):
        # 100 ue, GF 110, half bridge: ratio = -GF*eps/2
        self.assertAlmostEqual(float(strain_to_ratio(100e-6, 110, "half")), -0.0055)

    def test_beam_model_hand_calculation(self):
        beam = BeamModel(AppConfig().beam)        # 25x25 mm, L=500, x=50, E=70 GPa
        I = 0.025 ** 4 / 12
        self.assertAlmostEqual(beam.I, I)
        F = 9.80665
        eps = F * 0.45 * 0.0125 / (70e9 * I)
        self.assertAlmostEqual(float(beam.strain_from_force(F)), eps, places=12)
        self.assertAlmostEqual(float(beam.strain_from_mass(1000.0)), eps, places=12)
        self.assertAlmostEqual(float(beam.force_from_strain(eps)), F, places=9)
        d1 = float(beam.tip_deflection_from_force(F))
        d2 = 2 * 0.5 ** 3 * eps / (3 * 0.025 * 0.45)
        self.assertAlmostEqual(float(beam.tip_deflection_from_strain(eps)), d1, places=12)
        self.assertAlmostEqual(d1, d2, places=12)
        self.assertAlmostEqual(beam.natural_frequency_hz(), 82.2, delta=0.3)

    def test_processor_tare_and_calibration(self):
        cfg = AppConfig()
        proc = SignalProcessor(cfg)
        s = cfg.strain
        v = float(strain_to_ratio(50e-6, s.gauge_factor, "half")) * s.ina_gain * s.excitation_v
        out = proc.process_batch([0.0], [v], [0.0], [22.0])
        self.assertAlmostEqual(out["strain_ue"][0], 50.0, places=6)
        proc.set_tare(out["bridge_ratio"][0], 0.0, 22.0)
        self.assertAlmostEqual(proc.process_batch([0.0], [v], [0.0], [22.0])["strain_ue"][0], 0.0, places=6)
        proc.clear_tare()
        cfg.calibration.enabled = True
        cfg.calibration.coefficients = [2.0, 1.0]
        self.assertAlmostEqual(proc.process_batch([0.0], [v], [0.0], [22.0])["strain_ue"][0], 101.0, places=6)

    def test_temperature_compensation(self):
        cfg = AppConfig()
        cfg.temperature.compensation_enabled = True
        cfg.temperature.alpha_ue_per_c = 2.0
        proc = SignalProcessor(cfg)
        proc.set_tare(0.0, 0.0, 20.0)
        out = proc.process_batch([0, 1], [0.0, 0.0], [0.0, 0.0], [20.0, 25.0])
        np.testing.assert_allclose(out["strain_tc_ue"], [0.0, -10.0])
        out = proc.process_batch([0], [0.0], [0.0], [float("nan")])
        self.assertEqual(out["strain_tc_ue"][0], 0.0)   # NaN temperature: no correction, no NaN

    def test_spectrum_finds_sine(self):
        rng = np.random.default_rng(1)
        t = np.cumsum(rng.uniform(0.0015, 0.0025, 4000))   # jittered sampling ~500 Hz
        x = np.sin(2 * np.pi * 82.0 * t) + 0.1 * rng.normal(size=t.size)
        f, a, fs = amplitude_spectrum(t, x)
        self.assertAlmostEqual(dominant_frequency(f, a), 82.0, delta=0.5)
        self.assertGreater(fs, 400)

    def test_fit_polynomial(self):
        x = np.linspace(0, 100, 11)
        c, r2 = fit_polynomial(x, 1.06 * x + 0.5, 1)
        np.testing.assert_allclose(c, [1.06, 0.5], atol=1e-9)
        self.assertAlmostEqual(r2, 1.0)
        with self.assertRaises(ValueError):
            fit_polynomial([1.0], [2.0], 1)


class TestDS18B20(unittest.TestCase):
    def test_reads_sysfs_and_w1_slave(self):
        with tempfile.TemporaryDirectory() as d:
            dev = Path(d) / "28-0000abc"
            dev.mkdir()
            (dev / "temperature").write_text("23125\n")
            self.assertAlmostEqual(DS18B20(str(Path(d) / "28-*")).read_celsius(), 23.125)
            (dev / "temperature").unlink()
            (dev / "w1_slave").write_text("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n")
            self.assertAlmostEqual(DS18B20(str(Path(d) / "28-*")).read_celsius(), 23.125)
            (dev / "w1_slave").write_text("xx : crc=00 NO\nxx t=23125\n")
            self.assertTrue(math.isnan(DS18B20(str(Path(d) / "28-*")).read_celsius()))

    def test_missing_sensor(self):
        s = DS18B20("/nonexistent/28-*")
        self.assertFalse(s.available)
        self.assertTrue(math.isnan(s.read_celsius()))


class TestSimulatorController(unittest.TestCase):
    """Runs the simulator faster than real time (realtime=False) through the full controller."""

    def _run(self, ctl, seconds):
        # non-realtime simulator: drive by polling until simulated time advances
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            time.sleep(0.02)
            ctl.poll()

    def test_full_experiment(self):
        cfg = AppConfig()
        ctl = ExperimentController(cfg)
        ctl.start(simulate=True, seed=3, realtime=True)
        try:
            self._run(ctl, 1.0)
            self.assertTrue(ctl.running)
            self.assertGreater(ctl.sample_rate_hz, 100)
            ctl.tare()
            self._run(ctl, 0.3)
            self.assertLess(abs(ctl.latest_values()["strain_ue"]), 0.5)

            ctl.start_recording()
            for mass in (0, 500, 1000, 2000):
                ctl.set_sim_mass(mass)
                self._run(ctl, 0.8)
                ctl.capture_calibration_point(mass, seconds=0.4)
            ctl.set_sim_mass(0)
            ctl.sim_tap(5.0) if False else ctl.sim_tap()
            self._run(ctl, 1.5)
            n = ctl.stop_recording()
            self.assertGreater(n, 500)

            res = ctl.fit_calibration(1)
            self.assertGreater(res.r2, 0.995)
            # simulator gauge reads 6 % high -> correction slope about 1/1.06
            self.assertAlmostEqual(res.coefficients[0], 1 / 1.06, delta=0.03)
            ctl.apply_calibration(res.coefficients)
            ctl.set_sim_mass(1000)
            self._run(ctl, 0.8)
            theory = float(ctl.processor.beam.strain_from_mass(1000)) * 1e6
            self.assertAlmostEqual(ctl.latest_values(0.3)["strain_ue"], theory, delta=0.08 * theory + 1.6)

            df = ctl.recording_dataframe()
            spec = spectrum(df, "pzt_mV")
            self.assertAlmostEqual(spec.peak_hz, ctl.frontend.f1, delta=3.0)
            with tempfile.TemporaryDirectory() as d:
                csv, meta = ctl.save_recording(Path(d) / "run.csv")
                back, md = load_recording(csv)
                self.assertEqual(len(back), len(df))
                self.assertEqual(md["mode"], "simulation")
                self.assertFalse(summary_table(back).empty)
        finally:
            ctl.stop()
        self.assertFalse(ctl.running)

    def test_drift_fit_on_simulated_data(self):
        cfg = AppConfig()
        sim = SimulatedFrontEnd(cfg, seed=0, realtime=False)
        sim.tap_interval_s = 1e9
        sim.period = 0.01                                # 20 000 samples = 200 s of simulated time
        sim.open()
        rows = [sim.read_sample() for _ in range(20000)]
        import pandas as pd
        t = np.array([r.t for r in rows])
        temp = np.array([sim.temperature_at(x) for x in t])
        proc = SignalProcessor(cfg)
        out = proc.process_batch(t, [r.v_strain for r in rows], [r.v_pzt for r in rows], temp)
        drift = temperature_drift(pd.DataFrame(out))
        self.assertAlmostEqual(drift.alpha_ue_per_c, 1.5 * 1.06, delta=0.1)
        self.assertTrue(drift.reliable)
        # same data with load steps on top: the fit must be flagged as unreliable
        loaded = pd.DataFrame(out)
        loaded.loc[loaded.index[::2], "strain_raw_ue"] += 40.0
        self.assertFalse(temperature_drift(loaded).reliable)

    def test_single_channel_modes(self):
        for mode in ("strain", "pzt"):
            cfg = AppConfig()
            cfg.daq.channel_mode = mode
            sim = SimulatedFrontEnd(cfg, seed=0, realtime=False)
            sim.open()
            s = sim.read_sample()
            self.assertEqual(math.isnan(s.v_strain), mode == "pzt")
            self.assertEqual(math.isnan(s.v_pzt), mode == "strain")

    def test_settings_locked_while_running(self):
        ctl = ExperimentController()
        ctl.start(simulate=True, seed=1)
        try:
            with self.assertRaises(RuntimeError):
                ctl.update_config(AppConfig())
        finally:
            ctl.stop()
        ctl.update_config(AppConfig())

    def test_datastore_window(self):
        st = DataStore(100)
        proc = SignalProcessor(AppConfig())
        st.append(proc.process_batch(np.arange(150) * 0.01, np.zeros(150), np.zeros(150), np.full(150, 21.0)))
        self.assertEqual(len(st), 100)
        a = st.arrays(["t_s"], last_seconds=0.1)
        self.assertEqual(a["t_s"].size, 11)


if __name__ == "__main__":
    unittest.main()
