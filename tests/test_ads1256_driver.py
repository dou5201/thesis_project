"""Tests the real ADS1256 driver against an emulated chip (fake spidev + lgpio modules)."""
import sys
import types
import unittest

from cantilever_daq.config import AppConfig
from cantilever_daq.hardware import ADS1256FrontEnd, HardwareError

VREF = 2.5


def volts_to_code(v):
    code = int(round(v / (2 * VREF) * 0x7FFFFF))
    return code & 0xFFFFFF


class FakeADS1256:
    def __init__(self, chip_id=3):
        self.regs = {0: (chip_id << 4) | 0x01, 1: 0x01, 2: 0x20, 3: 0xF0}
        self.pending = None
        self.inputs = {0x01: 0.123456, 0x23: -0.25}   # MUX value -> differential volts
        self.commands = []

    def xfer2(self, data):
        data = list(data)
        if self.pending is not None:
            kind, arg = self.pending
            self.pending = None
            if kind == "rreg":
                return [self.regs[arg]]
            code = volts_to_code(self.inputs[self.regs[1]])
            return [(code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF]
        d = data[0]
        self.commands.append(d)
        if 0x50 <= d < 0x60:
            self.regs[d & 0x0F] = data[2]
        elif 0x10 <= d < 0x20:
            self.pending = ("rreg", d & 0x0F)
        elif d == 0x01:
            self.pending = ("rdata", None)
        return [0] * len(data)


def install_fakes(chip, drdy_level=0, label="pinctrl-rp1"):
    lg = types.ModuleType("lgpio")
    lg.levels = {}
    lg.gpiochip_open = lambda n: 100 + n if n < 2 else (_ for _ in ()).throw(OSError("no chip"))
    lg.gpio_get_chip_info = lambda h: (0, 54, f"gpiochip{h - 100}", label if h == 101 else "other")
    lg.gpiochip_close = lambda h: None
    lg.gpio_claim_input = lambda h, pin: None
    lg.gpio_claim_output = lambda h, pin, level=0: lg.levels.__setitem__(pin, level)
    lg.gpio_write = lambda h, pin, level: lg.levels.__setitem__(pin, level)
    lg.gpio_read = lambda h, pin: drdy_level
    lg.gpio_free = lambda h, pin: None

    sp = types.ModuleType("spidev")

    class SpiDev:
        def open(self, bus, dev):
            self.bus, self.dev = bus, dev

        def close(self):
            pass

        def xfer2(self, data):
            return chip.xfer2(data)

    sp.SpiDev = SpiDev
    sys.modules["lgpio"] = lg
    sys.modules["spidev"] = sp
    return lg


class TestADS1256Driver(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("lgpio", None)
        sys.modules.pop("spidev", None)

    def test_open_configure_and_read(self):
        chip = FakeADS1256()
        lg = install_fakes(chip)
        cfg = AppConfig().daq
        fe = ADS1256FrontEnd(cfg)
        fe.open()
        try:
            self.assertEqual(chip.regs[0], 0x04)          # buffer off, auto-cal on
            self.assertEqual(chip.regs[2], 0x00)          # PGA 1, clock out off
            self.assertEqual(chip.regs[3], 0xB0)          # 2000 SPS
            self.assertIn(0xF0, chip.commands)            # self calibration ran
            s = fe.read_sample()
            self.assertAlmostEqual(s.v_strain, 0.123456, places=5)
            self.assertAlmostEqual(s.v_pzt, -0.25, places=5)
            self.assertGreaterEqual(s.t, 0.0)
            self.assertEqual(lg.levels[cfg.pin_cs], 1)    # CS released after every frame
            self.assertIn(0xFC, chip.commands)            # SYNC after mux change
        finally:
            fe.close()

    def test_single_channel_mode(self):
        chip = FakeADS1256()
        install_fakes(chip)
        cfg = AppConfig().daq
        cfg.channel_mode = "pzt"
        fe = ADS1256FrontEnd(cfg)
        fe.open()
        s = fe.read_sample()
        fe.close()
        self.assertAlmostEqual(s.v_pzt, -0.25, places=5)
        self.assertNotEqual(s.v_strain, s.v_strain)       # NaN

    def test_wrong_chip_id_is_reported(self):
        install_fakes(FakeADS1256(chip_id=0))
        with self.assertRaises(HardwareError) as ctx:
            ADS1256FrontEnd(AppConfig().daq).open()
        self.assertIn("not detected", str(ctx.exception))

    def test_drdy_timeout_is_reported(self):
        install_fakes(FakeADS1256(), drdy_level=1)
        with self.assertRaises(HardwareError) as ctx:
            ADS1256FrontEnd(AppConfig().daq, drdy_timeout_s=0.05).open()
        self.assertIn("DRDY", str(ctx.exception))

    def test_missing_libraries(self):
        sys.modules["lgpio"] = None   # makes "import lgpio" fail
        with self.assertRaises(HardwareError):
            ADS1256FrontEnd(AppConfig().daq).open()


if __name__ == "__main__":
    unittest.main()
