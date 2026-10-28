"""GUI smoke test: drives every button of the real DAQApp against the strict Tk stand-in.

If a real Tk is present this test still uses the stand-in (it never opens a window).
Figures rendered during the test are written to tests/_artifacts/ for visual inspection.
"""
import logging
import tempfile
import unittest
from pathlib import Path

import tests.tk_stub as tk_stub

DIALOGS = tk_stub.install()

from cantilever_daq.acquisition import ExperimentController  # noqa: E402
from cantilever_daq.config import AppConfig  # noqa: E402
from cantilever_daq.gui import DAQApp  # noqa: E402

ART = Path(__file__).parent / "_artifacts"


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TestGuiSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = _Collect()
        logging.getLogger("cantilever_daq").addHandler(self.log)
        DIALOGS.errors.clear()
        DIALOGS.yesno_answers.clear()
        DIALOGS.paths.clear()
        self.root = tk_stub.Tk()
        self.app = DAQApp(self.root, ExperimentController(AppConfig()), prefer_simulation=True,
                          settings_path=self.dir / "settings.json")

    def tearDown(self):
        self.app.ctl.shutdown()
        logging.getLogger("cantilever_daq").removeHandler(self.log)
        self.tmp.cleanup()

    def unexpected(self):
        """Errors logged by periodic tasks or shown in dialogs that the test did not expect."""
        return [r.getMessage() for r in self.log.records if "periodic task" in r.getMessage()] + \
               [f"{t}: {m}" for t, m in DIALOGS.errors]

    def test_full_session(self):
        app, root = self.app, self.root
        self.assertEqual(app.mode_var.get(), "simulation")
        self.assertTrue(app.btn_stop.instate(["disabled"]))

        # ---- start + live view
        app.on_start()
        self.assertTrue(app.ctl.running and app.ctl.simulation)
        self.assertTrue(app.btn_start.instate(["disabled"]))
        root.pump(1.2)
        self.assertNotEqual(app.readout_vars["strain_ue"].get(), "—")
        self.assertGreater(app.live_canvas.draw_count, 3)
        app.on_tare()
        root.pump(0.4)
        self.assertLess(abs(float(app.readout_vars["strain_ue"].get())), 0.6)

        # ---- record + calibration with the simulated tip mass
        app.on_record()
        self.assertTrue(app.ctl.recording)
        app.cal_secs_var.set("0.5")
        for mass in ("0", "500", "1000", "2000"):
            app.cal_mass_var.set(mass)
            app.on_capture()
            self.assertTrue(app.btn_capture.instate(["disabled"]))
            root.pump(1.7)
        self.assertEqual(len(app.ctl.cal_points), 4, self.unexpected())
        self.assertEqual(len(app.cal_tree.get_children()), 4)
        app.cal_order_var.set(1)
        app.on_cal_fit()
        self.assertIn("R²", app.cal_result_var.get())
        app.on_cal_apply()
        self.assertTrue(app.ctl.config.calibration.enabled)
        self.assertTrue((self.dir / "settings.json").exists())
        app.cal_tree.selection_set(app.cal_tree.get_children()[0])
        app.on_cal_remove()
        self.assertEqual(len(app.ctl.cal_points), 3)

        app.sim_mass_var.set(0)
        app.on_sim_mass()
        app.ctl.sim_tap()
        root.pump(1.5)
        ART.mkdir(exist_ok=True)
        app.live_fig.savefig(ART / "live.png", dpi=90)
        app.cal_fig.savefig(ART / "calibration.png", dpi=90)
        app.on_record()
        self.assertFalse(app.ctl.recording)
        self.assertFalse(app.btn_save.instate(["disabled"]))

        # ---- temperature compensation toggle
        app.tc_var.set(True)
        app.on_toggle_tc()
        self.assertTrue(app.ctl.config.temperature.compensation_enabled)

        # ---- analysis
        app.on_analyse_current()
        text = app.ana_text.get("1.0", "end")
        self.assertIn("Natural frequency", text)
        self.assertIn("strain_ue", text)
        app.ana_fig.savefig(ART / "analysis.png", dpi=90)
        DIALOGS.paths.append(str(self.dir / "fig.png"))
        app.on_save_figure()
        self.assertTrue((self.dir / "fig.png").exists())

        # ---- save recording (CSV), then reopen it in the analysis tab
        DIALOGS.paths.append(str(self.dir / "run.csv"))
        app.on_save_recording()
        self.assertTrue((self.dir / "run.csv").exists() and (self.dir / "run.json").exists())
        self.assertFalse(app._unsaved)
        DIALOGS.paths.append(str(self.dir / "run.csv"))
        app.on_open_recording()
        self.assertIn("run.csv", app.ana_source_var.get())

        # ---- settings are locked while running
        app.setting_vars[("beam", "length_mm")].set("400")
        app.on_apply_settings()
        self.assertEqual(DIALOGS.errors.pop()[0], "Settings")
        app.on_stop()
        self.assertFalse(app.ctl.running)
        self.assertTrue(app.btn_start.instate(["!disabled"]))
        app.on_apply_settings()
        self.assertEqual(app.ctl.config.beam.length_mm, 400.0)
        self.assertIn("f₁", app.beam_info_var.get())

        # invalid number is rejected with a message, config unchanged
        app.setting_vars[("beam", "width_mm")].set("abc")
        app.on_apply_settings()
        self.assertEqual(DIALOGS.errors.pop()[0], "Settings")
        self.assertEqual(app.ctl.config.beam.width_mm, 25.0)
        app.setting_vars[("beam", "width_mm")].set("25")

        # save / load / reset settings files
        DIALOGS.paths.append(str(self.dir / "s.json"))
        app.on_save_settings()
        self.assertTrue((self.dir / "s.json").exists())
        DIALOGS.paths.append(str(self.dir / "s.json"))
        app.on_load_settings()
        DIALOGS.yesno_answers.append(True)
        app.on_reset_settings()
        self.assertEqual(app.setting_vars[("beam", "length_mm")].get(), "500.0")

        # ---- hardware mode without hardware -> offers simulation fallback
        app.mode_var.set("hardware")
        DIALOGS.yesno_answers.append(True)
        app.on_start()
        self.assertTrue(app.ctl.running and app.ctl.simulation)
        self.assertEqual(app.mode_var.get(), "simulation")
        root.pump(0.3)
        app.on_stop()

        # ---- quitting asks about unsaved data
        app._unsaved = True
        DIALOGS.yesno_answers.append(False)
        app.on_close()
        self.assertFalse(root.destroyed)
        DIALOGS.yesno_answers.append(True)
        app.on_close()
        self.assertTrue(root.destroyed)

        self.assertEqual(self.unexpected(), [])

    def test_file_mode(self):
        app, root = self.app, self.root
        data = self.dir / "sd_log.csv"
        data.write_text("t_s,strain_ue,pzt_mV,temp_C\n" +
                        "".join(f"{i * 0.01:.2f},{50 + i % 3},{i % 5},21.0\n" for i in range(300)))
        DIALOGS.paths.append(str(data))
        app.on_choose_file()
        self.assertEqual(app.mode_var.get(), "file")
        self.assertIn("sd_log.csv", app.file_var.get())
        app.on_start()
        self.assertTrue(app.ctl.running)
        self.assertEqual(app.mode_label.cget("text"), "FILE")
        root.pump(0.6)
        self.assertAlmostEqual(float(app.readout_vars["strain_ue"].get()), 51.0, delta=1.0)
        self.assertNotEqual(app.readout_vars["stress_MPa"].get(), "—")
        with open(data, "a") as fh:                      # the logger keeps writing
            fh.write("".join(f"{3 + i * 0.01:.2f},{200 + i % 3},0,21.5\n" for i in range(100)))
        root.pump(0.6)
        self.assertEqual(app.ctl.total_samples, 400)
        self.assertAlmostEqual(float(app.readout_vars["temp_C"].get()), 21.5, places=2)
        app.on_stop()
        # the same generic file opens on the Analysis tab
        DIALOGS.paths.append(str(data))
        app.on_open_recording()
        self.assertIn("sd_log.csv", app.ana_source_var.get())
        # a missing file is reported, no simulation fallback is offered
        app.ctl.config.file_source.path = str(self.dir / "missing.csv")
        app.on_start()
        self.assertEqual(DIALOGS.errors.pop()[0], "Cannot read data file")
        self.assertFalse(app.ctl.running)
        self.assertEqual(self.unexpected(), [])

    def test_errors_are_reported_not_raised(self):
        app = self.app
        app.on_tare()                      # not running
        app.on_capture()                   # not running -> capture has no data
        self.root.pump(2.6)
        app.on_cal_fit()                   # no points
        app.on_analyse_current()           # nothing recorded
        app.on_use_alpha()                 # no drift fit
        app.on_save_recording()            # nothing recorded
        titles = [t for t, _ in DIALOGS.errors]
        self.assertEqual(titles, ["Tare", "Capture", "Fit", "Analysis", "Thermal drift", "Save recording"])


if __name__ == "__main__":
    unittest.main()
