"""Probing a silent channel is a register write, so it lives inside the cleanup.

`calibrate --probe` puts a channel that reads 0 RPM into manual mode at full
duty to find out whether a fan is attached to it. That is hardware-mutating,
and it used to run in front of the try/finally whose aggregate safe_state() is
the verdict every calibration exit status is built on. The only thing behind
it was _Borrowed's restore(), which wrote the old values back and checked
nothing at all.

So a chip that accepted the probe and then refused automatic mode left
calibration through one of two doors - "no controllable fan channel is
spinning", and "the cached calibration already matches" - with a channel in
manual mode, no aggregate handback, and, on the second door, exit 0.

Everything here runs the shipped code against a disposable fake sysfs tree. No
/sys path is touched and no fan turns. The sweep itself is measured in
test_fan_safety.py; the stand-in below writes registers and returns numbers,
so that these tests stay about which scope those writes happen in.
"""

import contextlib
import io
import os
import tempfile
import unittest

from helpers import load_fan, write_sysfs_tree

fan = load_fan()


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


def make_chip(root, channels, rpm=0, pwm=160, enable=1, name="f71882fg.2592"):
    """A fake platform device whose fans are silent and in manual mode.

    Both defaults matter. rpm=0 is what makes --probe do anything, and
    enable=1 is what makes automatic mode something this program has to put
    there: a channel that starts at 2 cannot tell a verified handback from a
    restore that quietly wrote the old value back.
    """
    spec = {}
    for channel in channels:
        current = rpm.get(channel, 0) if isinstance(rpm, dict) else rpm
        spec["%s/fan%d_input" % (name, channel)] = "%d\n" % current
        spec["%s/pwm%d" % (name, channel)] = "%d\n" % pwm
        spec["%s/pwm%d_enable" % (name, channel)] = "%s\n" % enable
    write_sysfs_tree(root, spec)
    return os.path.join(root, name)


@contextlib.contextmanager
def chip(device, ignore=(), fail=(), refuse=(), spins=()):
    """Registers, plus a tachometer that answers a probe on `spins`.

    ignore   registers that accept a write and keep their old contents, which
             is what a chip that will not leave manual mode does
    fail     registers whose write reports failure, as an EIO would
    refuse   (register, value) pairs whose write reports failure. `fail`
             cannot express "takes manual mode and refuses automatic mode",
             which is the chip a probe is most dangerous on: it accepts being
             spun up and then will not be put back
    spins    channels whose fan starts turning once full duty is commanded
    """
    real = fan.write_sysfs
    written = []

    def write(path, value):
        name = os.path.basename(path)
        written.append((name, int(value)))
        if (name, int(value)) in [(r, v) for r, v in refuse]:
            return False
        if name in fail:
            return False
        if name in ignore:
            return True
        result = real(path, value)
        for channel in spins:
            if name == "pwm%d" % channel:
                with open(os.path.join(device, "fan%d_input" % channel), "w",
                          encoding="ascii") as fh:
                    fh.write("%d\n" % (1200 if int(value) >= fan.PWM_FLOOR
                                       else 0))
        return result

    fan.write_sysfs = write
    try:
        yield written
    finally:
        fan.write_sysfs = real


class ProbeScopeCase(unittest.TestCase):
    """A silent chip, a fast probe, and a sweep that only writes registers."""

    CHANNELS = (3,)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, self.CHANNELS)
        self.glob = os.path.join(self.tmp.name, "f71882fg.*")
        self.cache = os.path.join(self.tmp.name, "state", "fan-calibration.json")
        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")
        real = os.geteuid
        os.geteuid = lambda: 0
        self.addCleanup(setattr, os, "geteuid", real)
        self.addCleanup(fan.reset_handback_record)
        real_sensor = fan.find_temp_input
        fan.find_temp_input = lambda sensor: self.sensor
        self.addCleanup(setattr, fan, "find_temp_input", real_sensor)

        # The same discovery logic, without its four-second settle.
        real_detect = fan.FanController.detect_active

        def detect_active(controller, probe=False, settle=4.0):
            return real_detect(controller, probe=probe, settle=0)

        fan.FanController.detect_active = detect_active
        self.addCleanup(setattr, fan.FanController, "detect_active",
                        real_detect)

        real_calibrate = fan.calibrate_channel

        def calibrate_channel(controller, channel, temp_path, settle=0, spin=0):
            controller.set_manual(channel)
            controller.set_pwm(channel, 90)
            return {"max_rpm": 1500, "min_rpm": 400, "stall_duty": 85,
                    "minstop": 95, "minstart": 120}

        fan.calibrate_channel = calibrate_channel
        self.addCleanup(setattr, fan, "calibrate_channel", real_calibrate)

    def mode(self, channel):
        with open(os.path.join(self.device, "pwm%d_enable" % channel),
                  encoding="ascii") as fh:
            return fh.read().strip()

    def calibrate(self, *extra, **kwargs):
        argv = ["calibrate", "--yes", "--probe", "--device-glob", self.glob,
                "--cache", self.cache] + list(extra)
        with quiet() as out, chip(self.device, **kwargs) as written:
            code = fan.main(argv)
        return code, written, out.getvalue()

    def save_cache(self, channels):
        fan.save_cache({"version": 1, "channels": list(channels),
                        "fans": {str(c): {"minstop": 95, "minstart": 120,
                                          "min_rpm": 400, "max_rpm": 1500}
                                 for c in channels}}, self.cache)


class ProbeWithoutAFanTests(ProbeScopeCase):
    """The probe ran, nothing was spinning, and calibration gave up."""

    def test_a_probe_that_finds_nothing_still_ends_in_a_verified_handback(self):
        # The channel was put in manual mode at full duty to ask the question.
        # Answering "no fan here" is not permission to leave it there.
        code, written, printed = self.calibrate()

        self.assertEqual(code, 1, printed)
        self.assertIn("no controllable fan channel is spinning", printed)
        self.assertIn("SAFE-STATE calibration", printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC),
                         "a probed channel was left in manual mode")
        self.assertEqual(written[-2:], [("pwm3", fan.SAFE_PWM),
                                        ("pwm3_enable", fan.PWM_AUTOMATIC)],
                         "the probe was not covered by the safe state: %s"
                         % written)

    def test_a_probe_the_chip_will_not_undo_is_reported_and_fatal(self):
        # Accepted probe writes, rejected automatic mode: the combination an
        # unchecked restore() reported as success. The chip starts in
        # automatic mode here, so putting it back means writing the value it
        # refuses - which is what a channel spun up by a probe needs.
        self.device = make_chip(self.tmp.name, self.CHANNELS, enable=2)
        code, _, printed = self.calibrate(
            refuse=[("pwm3_enable", fan.PWM_AUTOMATIC)])

        self.assertEqual(code, 1, printed)
        self.assertIn("was borrowed and not proven back", printed)
        self.assertIn("could not be confirmed back in automatic mode", printed)
        self.assertIn("NOT proven back in the chip's automatic mode", printed)
        self.assertEqual(self.mode(3), str(fan.PWM_MANUAL),
                         "the fixture did not model a chip stuck in manual")
        self.assertFalse(os.path.exists(self.cache))
        self.assertNotIn("Start the service when you are ready", printed)
        self.assertTrue(fan.handback_unverified())

    def test_a_probe_the_chip_refuses_outright_is_still_handed_back(self):
        # The duty register refuses, so the channel is not treated as active -
        # and it was still put into manual mode to ask.
        code, _, printed = self.calibrate(fail=["pwm3"])

        self.assertEqual(code, 1, printed)
        self.assertIn("did not accept the probe", printed)
        self.assertIn("SAFE-STATE calibration", printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC))


class PartialProbeTests(ProbeScopeCase):
    """Two silent channels, one fan. Both were touched, so both go back."""

    CHANNELS = (3, 5)

    def test_a_channel_that_did_not_spin_up_is_still_handed_back(self):
        code, written, printed = self.calibrate(spins=[3])

        self.assertEqual(code, 0, printed)
        self.assertIn("channels to calibrate: [3]", printed)
        self.assertEqual(self.mode(5), str(fan.PWM_AUTOMATIC),
                         "the channel that was probed and rejected was left "
                         "in manual mode")
        self.assertIn(("pwm5_enable", fan.PWM_AUTOMATIC), written)
        self.assertTrue(os.path.exists(self.cache), printed)

    def test_a_rejected_channel_that_will_not_go_back_fails_the_run(self):
        # Channel 3 calibrates perfectly. Channel 5 was only ever probed, is
        # not in the results, and will not leave manual mode - and that is
        # still a fan nobody is in charge of.
        code, _, printed = self.calibrate(spins=[3], ignore=["pwm5_enable"])

        self.assertEqual(code, 1, printed)
        self.assertIn("1 of 2 channels could not be confirmed", printed)
        self.assertIn("NOT saved", printed)
        self.assertFalse(os.path.exists(self.cache),
                         "a calibration was saved with a channel left in "
                         "manual mode")
        self.assertNotIn("Start the service when you are ready", printed)


class ProbeWithAnUpToDateCacheTests(ProbeScopeCase):
    """The early return that used to skip the cleanup entirely."""

    def test_an_up_to_date_cache_still_hands_the_probed_channel_back(self):
        self.save_cache([3])

        code, written, printed = self.calibrate(spins=[3])

        self.assertEqual(code, 0, printed)
        self.assertIn("cached calibration already matches", printed)
        self.assertIn("SAFE-STATE calibration not needed", printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC),
                         "the probe was left in manual mode by the early exit")
        self.assertIn(("pwm3_enable", fan.PWM_AUTOMATIC), written)

    def test_an_up_to_date_cache_does_not_excuse_a_failed_handback(self):
        self.save_cache([3])

        code, _, printed = self.calibrate(spins=[3], ignore=["pwm3_enable"])

        self.assertEqual(code, 1, printed)
        self.assertIn("could not be confirmed back in automatic mode", printed)
        self.assertIn("NOT saved", printed)

    def test_a_run_that_wrote_no_register_has_nothing_to_hand_back(self):
        # --channels names them, so nothing is probed and the cache matches:
        # not one register was written. "No target" is a failure inside
        # safe_state() because a caller asked for a handback and got none.
        # This is the other case, and it is not one.
        self.save_cache([3])
        argv = ["calibrate", "--yes", "--channels", "3", "--device-glob",
                self.glob, "--cache", self.cache]
        with quiet() as out, chip(self.device) as written:
            code = fan.main(argv)

        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("nothing to hand back", out.getvalue())
        self.assertEqual(written, [], "a register was written after all")
        self.assertFalse(fan.handback_unverified())


class ProbeControlTests(ProbeScopeCase):
    """The machine these scripts are written for: a probe that works."""

    def test_a_probe_that_finds_a_fan_calibrates_and_exits_zero(self):
        code, written, printed = self.calibrate(spins=[3])

        self.assertEqual(code, 0, printed)
        self.assertIn("channels to calibrate: [3]", printed)
        self.assertTrue(os.path.exists(self.cache), printed)
        self.assertIn("Start the service when you are ready", printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC))
        self.assertEqual(written[-2:], [("pwm3", fan.SAFE_PWM),
                                        ("pwm3_enable", fan.PWM_AUTOMATIC)])
        self.assertFalse(fan.handback_unverified())


if __name__ == "__main__":
    unittest.main()
