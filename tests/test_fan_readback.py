"""A register that takes the write and keeps its old contents.

This is the failure mode that looks like everything working. `write()` on a
sysfs attribute returns success as soon as the driver has accepted the bytes;
what the chip then does with them is not in that answer. A `pwmN_enable` that
stays on the chip's own automatic curve, or a `pwmN` that holds the duty from
the previous command, reports nothing at all to a program that only checks the
write:

  * a calibration sweep measures the fan at a duty it was never moved to,
    records the stall point of a rung it never stood on, and then steps
    *lower* from there;
  * a control loop drives a curve against a register the chip is ignoring, and
    the per-register failure counter never moves because every write
    "succeeded";
  * the initial manual-mode acquisition succeeds on a channel this program
    does not own.

None of that is reachable with a plain file standing in for a register, which
is why the writes below are intercepted: `ignore` is a register that accepts a
write and keeps its contents, and `ignore_when` is the same thing aimed at one
step of a sweep. The reads go to the real file throughout, so what these tests
exercise is the program's own readback and nothing about the harness.

The rule under test is exact equality. A duty of 100 that reads back as 99 is
a failure here, deliberately: there is no documented quantization in this
driver, and a tolerance would forgive the chip this is aimed at.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import load_fan, write_sysfs_tree

fan = load_fan()


@contextlib.contextmanager
def quiet():
    out = io.StringIO()
    real = sys.stdout
    sys.stdout = out
    try:
        yield out
    finally:
        sys.stdout = real


def make_chip(root, channels=(3,), rpm=1400, pwm=160, enable=2,
              name="f71882fg.2592"):
    spec = {}
    for channel in channels:
        mode = enable.get(channel, 2) if isinstance(enable, dict) else enable
        spec["%s/fan%d_input" % (name, channel)] = "%d\n" % rpm
        spec["%s/pwm%d" % (name, channel)] = "%d\n" % pwm
        spec["%s/pwm%d_enable" % (name, channel)] = "%s\n" % mode
    write_sysfs_tree(root, spec)
    return os.path.join(root, name)


@contextlib.contextmanager
def registers(ignore=(), ignore_when=None, hook=None):
    """Stand in front of the module's writes for one test.

    ignore       registers that report success and keep their old contents
    ignore_when  called with (register, value); a true answer swallows that
                 one write. `ignore` cannot express "take the fourth step of
                 the descent and keep the third", which is where a silently
                 ignored duty is most dangerous: the sweep has a fan it
                 believes is one rung lower than it is.
    hook         called with (register, value) after each landed write

    Yields the ordered list of (register, value) writes attempted.
    """
    real = fan.write_sysfs
    written = []

    def write(path, value):
        name = os.path.basename(path)
        written.append((name, int(value)))
        if name in ignore:
            return True
        if ignore_when is not None and ignore_when(name, int(value)):
            return True
        result = real(path, value)
        if hook is not None:
            hook(name, int(value))
        return result

    fan.write_sysfs = write
    try:
        yield written
    finally:
        fan.write_sysfs = real


def nth_write(register, value, index=1):
    """An ignore_when predicate matching the `index`-th write of `value`.

    One duty is commanded more than once in a single sweep - 255 for the
    spin-up and again for the cool-down, the stall point again on the way back
    up - so "this register at this value" does not name one step of it.
    """
    seen = [0]

    def predicate(name, written):
        if name != register or written != value:
            return False
        seen[0] += 1
        return seen[0] == index

    return predicate


def as_root(case):
    real = os.geteuid
    os.geteuid = lambda: 0
    case.addCleanup(setattr, os, "geteuid", real)


def patched(case, name, value):
    real = getattr(fan, name)
    setattr(fan, name, value)
    case.addCleanup(setattr, fan, name, real)


class CommitWriteTests(unittest.TestCase):
    """The accounting itself: what a readback mismatch is counted as."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, [1, 3], enable=2)
        self.controller = fan.FanController(self.device)

    def test_an_ignored_mode_write_is_a_failed_write(self):
        with quiet() as out, registers(ignore=["pwm3_enable"]):
            self.assertFalse(self.controller.set_manual(3))
        self.assertEqual(self.controller.failure_count(3, "pwm3_enable"), 1)
        self.assertIn("did not take the value", out.getvalue())

    def test_an_ignored_duty_write_is_a_failed_write(self):
        with quiet() as out, registers(ignore=["pwm3"]):
            self.assertFalse(self.controller.set_pwm(3, 100))
        self.assertEqual(self.controller.failure_count(3, "pwm3"), 1)
        self.assertIn("did not take the value", out.getvalue())

    def test_a_register_that_will_not_read_back_is_a_failed_write(self):
        # The write lands somewhere; the read does not come back. That is a
        # chip saying less than "no", and it may not be read as "yes".
        with quiet() as out, registers(ignore=["pwm3"]):
            os.unlink(os.path.join(self.device, "pwm3"))
            self.assertFalse(self.controller.set_pwm(3, 100))
        self.assertEqual(self.controller.failure_count(3, "pwm3"), 1)
        self.assertIn("reads back as nothing", out.getvalue())

    def test_the_comparison_is_exact_and_not_a_tolerance(self):
        # One off is a failure. There is no documented quantization in this
        # driver, so a near miss is a chip doing something unexplained.
        def off_by_one(path, value):
            with open(path, "w", encoding="ascii") as fh:
                fh.write("%d\n" % (int(value) - 1))
            return True

        real = fan.write_sysfs
        fan.write_sysfs = off_by_one
        self.addCleanup(setattr, fan, "write_sysfs", real)
        with quiet() as out:
            self.assertFalse(self.controller.set_pwm(3, 100))
        self.assertIn("asked for 100, reads back as 99", out.getvalue())

    def test_the_clamped_value_is_what_is_required_back(self):
        # 9999 is a command for 255, and 255 is what the register then has to
        # hold. Comparing against the unclamped request would fail every
        # out-of-range command on a chip that behaved perfectly.
        with quiet():
            self.assertTrue(self.controller.set_pwm(3, 9999))
        self.assertEqual(self.controller.failure_count(3, "pwm3"), 0)

    def test_a_landed_write_still_succeeds(self):
        with quiet():
            self.assertTrue(self.controller.set_manual(3))
            self.assertTrue(self.controller.set_pwm(3, 100))
        self.assertEqual(self.controller.failure_count(3, "pwm3_enable"), 0)
        self.assertEqual(self.controller.failure_count(3, "pwm3"), 0)

    def test_a_dry_run_reads_nothing_back_and_records_nothing(self):
        controller = fan.FanController(self.device, dry_run=True)
        with quiet():
            self.assertTrue(controller.set_manual(3))
            self.assertTrue(controller.set_pwm(3, 100))
        self.assertEqual(controller.exhausted_writes(1), [])

    def test_one_ignored_register_reaches_the_limit_on_its_own(self):
        # The accounting shape that matters: channel 3 answers every command
        # while channel 1's mode register swallows every one of them.
        with quiet(), registers(ignore=["pwm1_enable"]):
            for _ in range(fan.WRITE_FAILURE_LIMIT):
                self.controller.set_manual(1)
                self.controller.set_pwm(3, 100)
        self.assertEqual(
            self.controller.exhausted_writes(fan.WRITE_FAILURE_LIMIT),
            [(1, "pwm1_enable", fan.WRITE_FAILURE_LIMIT)])


class CalibrationReadbackCase(unittest.TestCase):
    """A sweep against a fake chip, with the wall clock removed.

    The tachometer answers the duty register so the sweep converges where a
    real one would. With STALL_BELOW at 90 the steps are deterministic:

        pwm3_enable 1                      manual mode
        pwm3        255                    spin-up
        pwm3        120 115 ... 90 85      descending sweep, stalls at 85
        pwm3        0                      zero, for the restart search
        pwm3        75 80 85 90            restart search, restarts at 90
        pwm3        100                    final minimum
        pwm3        255                    cool-down
        pwm3 255, pwm3_enable 2            the safe state
    """

    STALL_BELOW = 90

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, [3], rpm=1400, pwm=160, enable=2)
        self.cache = os.path.join(self.tmp.name, "state", "fan-calibration.json")
        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")
        as_root(self)
        self.addCleanup(fan.reset_handback_record)
        patched(self, "find_temp_input", lambda sensor: self.sensor)

        real_rpm = fan.FanController.rpm

        def rpm(controller, channel):
            duty = controller.pwm(channel)
            return 0 if duty is None or duty < self.STALL_BELOW else 90 + duty * 6

        fan.FanController.rpm = rpm
        self.addCleanup(setattr, fan.FanController, "rpm", real_rpm)

        real_calibrate = fan.calibrate_channel
        patched(self, "calibrate_channel",
                lambda controller, channel, temp_path, settle=0.05, spin=0.05:
                real_calibrate(controller, channel, temp_path, settle, spin))

    def calibrate(self, **kwargs):
        argv = ["calibrate", "--yes", "--device-glob",
                os.path.join(self.tmp.name, "f71882fg.*"),
                "--cache", self.cache]
        with quiet() as out, registers(**kwargs) as written:
            code = fan.main(argv)
        return code, written, out.getvalue()

    @staticmethod
    def duties(written):
        return [value for name, value in written if name == "pwm3"]

    def mode(self):
        with open(os.path.join(self.device, "pwm3_enable"),
                  encoding="ascii") as fh:
            return fh.read().strip()


class CalibrationModeReadbackTests(CalibrationReadbackCase):
    """A chip that takes the manual-mode command and stays on its own curve."""

    def test_a_silently_ignored_manual_mode_write_aborts_the_sweep(self):
        code, written, printed = self.calibrate(
            ignore_when=nth_write("pwm3_enable", fan.PWM_MANUAL))

        self.assertEqual(code, 1, printed)
        self.assertIn("channel 3 is not in manual mode", printed)
        self.assertIn("did not take the value", printed)

    def test_no_duty_is_commanded_after_the_mode_mismatch(self):
        # Not one step of the sweep may run: every duty written to a channel
        # the chip is still driving itself is a command to nobody, and the
        # RPM read back afterwards describes the chip's decisions.
        code, written, printed = self.calibrate(
            ignore_when=nth_write("pwm3_enable", fan.PWM_MANUAL))

        self.assertEqual(code, 1, printed)
        self.assertEqual(self.duties(written), [fan.SAFE_PWM],
                         "a duty was commanded on a channel this program does "
                         "not own: %s" % written)

    def test_nothing_is_saved_and_the_fans_are_handed_back(self):
        code, written, printed = self.calibrate(
            ignore_when=nth_write("pwm3_enable", fan.PWM_MANUAL))

        self.assertEqual(code, 1, printed)
        self.assertFalse(os.path.exists(self.cache),
                         "a cache was saved after a mode readback mismatch")
        self.assertEqual(written[-1], ("pwm3_enable", fan.PWM_AUTOMATIC),
                         "the abort did not end in a safe state: %s" % written)
        self.assertEqual(self.mode(), str(fan.PWM_AUTOMATIC))

    def test_a_chip_that_takes_manual_mode_calibrates_normally(self):
        # The control. Same sweep, same chip, nothing swallowed.
        code, _, printed = self.calibrate()

        self.assertEqual(code, 0, printed)
        self.assertTrue(os.path.exists(self.cache), printed)
        self.assertNotIn("did not take the value", printed)


class CalibrationDutyReadbackTests(CalibrationReadbackCase):
    """Every phase of the sweep, and no lower command after a mismatch.

    Each case swallows exactly one duty write and then asserts that the sweep
    stopped there. "Stopped there" is the only assertion that matters: the
    next command in every one of these phases is either lower than the one
    that did not land, or a measurement of a fan whose duty is unknown.
    """

    def assert_aborted_at(self, duty, index=1):
        code, written, printed = self.calibrate(
            ignore_when=nth_write("pwm3", duty, index))

        self.assertEqual(code, 1, printed)
        self.assertIn("is not at the", printed)
        self.assertIn("did not take the value", printed)
        self.assertFalse(os.path.exists(self.cache),
                         "a cache was saved after a duty readback mismatch")
        # The safe state is the only thing allowed to write a duty afterwards,
        # and it writes full duty. Anything else is the sweep carrying on.
        duties = self.duties(written)
        self.assertEqual(duties[-1], fan.SAFE_PWM,
                         "the abort did not end at full duty: %s" % duties)
        self.assertEqual(self.mode(), str(fan.PWM_AUTOMATIC))
        return duties, printed

    def test_the_spin_up_duty(self):
        duties, printed = self.assert_aborted_at(fan.PWM_MAX)
        self.assertEqual(duties, [fan.PWM_MAX, fan.SAFE_PWM],
                         "the sweep continued past the spin-up: %s" % printed)

    def test_the_descending_sweep(self):
        # The fourth rung down. The next command would be 100, which is lower
        # than a duty the fan was never actually moved to.
        duties, _ = self.assert_aborted_at(105)
        self.assertNotIn(100, duties,
                         "the sweep stepped down past an unlanded duty: %s"
                         % duties)
        self.assertEqual(max(d for d in duties if d != fan.SAFE_PWM), 120)

    def test_the_zero_before_the_restart_search(self):
        # PWM_MIN is the one duty the fan is meant to stop at. A zero that did
        # not land leaves the restart search measuring a fan that never
        # stopped, so every "still stopped" line below it is false.
        duties, _ = self.assert_aborted_at(fan.PWM_MIN)
        self.assertEqual(duties[-2:], [fan.PWM_MIN, fan.SAFE_PWM],
                         "the restart search ran after an unlanded zero: %s"
                         % duties)

    def test_the_restart_search(self):
        # 75 is the first rung of the search back up, with the fan stopped.
        duties, _ = self.assert_aborted_at(75)
        self.assertNotIn(80, duties,
                         "the restart search stepped on past an unlanded "
                         "duty, with the fan stopped: %s" % duties)

    def test_the_final_minimum(self):
        duties, _ = self.assert_aborted_at(100)
        self.assertEqual(duties[-2:], [100, fan.SAFE_PWM],
                         "the sweep measured min_rpm at an unlanded duty: %s"
                         % duties)

    def test_the_cool_down(self):
        # The last write of the channel, and the one the next channel's sweep
        # starts behind. A chip that will not take full duty at the end of one
        # sweep is not one to start another on.
        duties, _ = self.assert_aborted_at(fan.PWM_MAX, index=2)
        self.assertEqual(duties[-2:], [fan.PWM_MAX, fan.SAFE_PWM], duties)

    def test_a_chip_that_takes_every_duty_calibrates_normally(self):
        code, written, printed = self.calibrate()

        self.assertEqual(code, 0, printed)
        cache = fan.load_cache(self.cache)
        self.assertEqual(cache["channels"], [3])
        self.assertNotIn("is not at the", printed)


class RuntimeReadbackTests(unittest.TestCase):
    """The control loop against a register that answers and does nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, [1, 3], rpm=1400, pwm=160,
                                enable=1)
        self.cache = os.path.join(self.tmp.name, "fan-calibration.json")
        with open(self.cache, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "channels": [1, 3],
                       "fans": {c: {"minstop": 90, "minstart": 120,
                                    "min_rpm": 400, "max_rpm": 1500}
                                for c in ("1", "3")}}, fh)
        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")
        as_root(self)
        self.addCleanup(fan.reset_handback_record)
        patched(self, "find_temp_input", lambda sensor: self.sensor)
        self.stop_after(40)

    def stop_after(self, iterations):
        """Backstop only: every test here ends for its own reason."""
        real_read = fan.read_temp
        seen = []

        def read_temp(path):
            seen.append(path)
            if len(seen) > iterations:
                raise fan.Terminated("signal SIGTERM")
            return real_read(path)

        patched(self, "read_temp", read_temp)

    def run_loop(self, **kwargs):
        argv = ["run", "--device-glob",
                os.path.join(self.tmp.name, "f71882fg.*"),
                "--cache", self.cache, "--interval", "0"]
        with quiet() as out, registers(**kwargs) as written:
            code = fan.main(argv)
        return code, written, out.getvalue()

    # -- the initial acquisition --------------------------------------------
    def test_an_ignored_manual_mode_write_never_enters_the_loop(self):
        # The chip is in automatic mode and stays there. Without a readback
        # this is a service that runs for months reporting a curve it is not
        # driving.
        self.device = make_chip(self.tmp.name, [1, 3], rpm=1400, pwm=160,
                                enable=2)
        code, written, printed = self.run_loop(ignore=["pwm1_enable"])

        self.assertEqual(code, 1, printed)
        self.assertIn("are not in manual mode", printed)
        self.assertIn("[1]", printed)
        duties = [v for name, v in written if name in ("pwm1", "pwm3")]
        self.assertEqual(set(duties), {fan.SAFE_PWM},
                         "a curve was driven against a channel the chip still "
                         "owns: %s" % duties)

    def test_both_channels_are_attempted_before_the_verdict(self):
        # Channel 3 has to be remembered even though channel 1 failed, or the
        # safe state on the way out has nothing to restore it from.
        self.device = make_chip(self.tmp.name, [1, 3], rpm=1400, pwm=160,
                                enable=2)
        code, written, printed = self.run_loop(ignore=["pwm1_enable"])

        self.assertEqual(code, 1, printed)
        self.assertIn(("pwm3_enable", fan.PWM_MANUAL), written)
        self.assertIn(("pwm3_enable", fan.PWM_AUTOMATIC), written)

    # -- the per-register threshold -----------------------------------------
    def test_one_ignored_duty_register_trips_the_limit_on_its_own(self):
        # The multi-channel shape: pwm1 accepts every write and keeps its
        # contents, pwm3 works perfectly. A counter that any success could
        # clear never notices, and the fan on channel 1 is held at whatever
        # duty it happened to be on for as long as the service runs.
        code, written, printed = self.run_loop(ignore=["pwm1"])

        self.assertEqual(code, 1, printed)
        self.assertIn("pwm1 refused %d consecutive writes"
                      % fan.WRITE_FAILURE_LIMIT, printed)
        self.assertIn("not accepting commands", printed)
        self.assertIn("did not take the value", printed)
        # The healthy channel was answering the whole time, which is exactly
        # what used to hide this.
        self.assertTrue([v for name, v in written if name == "pwm3"])

    def test_the_limit_triggers_at_the_documented_count(self):
        code, written, printed = self.run_loop(ignore=["pwm1"])

        self.assertEqual(code, 1, printed)
        ignored = [v for name, v in written if name == "pwm1"]
        # The sensor is pinned at 45 C, so the curve duty is not full duty and
        # the one SAFE_PWM write is the safe state on the way out.
        self.assertEqual(ignored[-1], fan.SAFE_PWM, ignored)
        self.assertEqual(len(ignored[:-1]), fan.WRITE_FAILURE_LIMIT,
                         "the loop ran on past the limit: %s" % ignored)

    def test_an_ignored_mode_register_mid_loop_trips_the_limit(self):
        # Only the initial acquisition writes pwmN_enable in a run, so this
        # aims at the duty register of channel 3 while channel 1 answers -
        # the mirror of the case above, proving the accounting is not keyed to
        # one channel number.
        code, _, printed = self.run_loop(ignore=["pwm3"])

        self.assertEqual(code, 1, printed)
        self.assertIn("pwm3 refused %d consecutive writes"
                      % fan.WRITE_FAILURE_LIMIT, printed)

    def test_a_chip_that_answers_runs_to_the_backstop(self):
        # The control: nothing swallowed, no threshold, a clean stop.
        code, _, printed = self.run_loop()

        self.assertEqual(code, 0, printed)
        self.assertNotIn("did not take the value", printed)
        self.assertNotIn("not accepting commands", printed)
        self.assertNotIn("not in manual mode", printed)


if __name__ == "__main__":
    unittest.main()
