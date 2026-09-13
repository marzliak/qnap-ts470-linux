"""The fan safe state has to be proven, and calibration may not run blind.

Everything here runs the shipped code against a disposable fake sysfs tree.
Nothing touches /sys, no fan turns, and the failures a real chip produces - a
write it rejects, a register that stops answering, a mode that silently stays
manual - are injected at the two functions that do the I/O, because a plain
file cannot model a register that accepts a write and ignores it.
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
    """Swallow the safe-state and calibration logging."""
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


def make_chip(root, channels, rpm=1000, pwm=160, enable=2, name="f71882fg.2592"):
    """A fake platform device. `enable` may be a dict for per-channel modes."""
    spec = {}
    for channel in channels:
        mode = enable.get(channel, 2) if isinstance(enable, dict) else enable
        spec["%s/fan%d_input" % (name, channel)] = "%d\n" % rpm
        spec["%s/pwm%d" % (name, channel)] = "%d\n" % pwm
        spec["%s/pwm%d_enable" % (name, channel)] = "%s\n" % mode
    write_sysfs_tree(root, spec)
    return os.path.join(root, name)


@contextlib.contextmanager
def registers(ignore=(), fail=(), hook=None, fail_when=None):
    """Stand in front of the module's writes for the duration of one test.

    ignore     registers that accept a write and keep their old contents,
               which is what a stuck or lying chip does and what a file cannot
    fail       registers whose write reports failure, as an EIO would
    fail_when  called with (register, value); a true answer fails that one
               write. `fail` cannot express "refuse the fourth step of the
               sweep and nothing else", which is what a gate in the middle of
               a descent has to be aimed at
    hook       called with (register, value) after each accepted write, to
               make something else happen at an exact point in a sweep

    Yields the ordered list of (register, value) writes attempted.
    """
    real = fan.write_sysfs
    written = []

    def write(path, value):
        name = os.path.basename(path)
        written.append((name, int(value)))
        if name in fail:
            return False
        if fail_when is not None and fail_when(name, int(value)):
            return False
        if name in ignore:
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
    """A fail_when predicate matching the `index`-th write of `value`.

    One duty can be commanded twice in a single calibration - 255 for the
    spin-up and again for the cool-down, the stall point again on the way back
    up - so "fail this register at this value" is not specific enough to aim
    at one gate.
    """
    seen = [0]

    def predicate(name, written):
        if name != register or written != value:
            return False
        seen[0] += 1
        return seen[0] == index

    return predicate


def as_root(case):
    """Pretend to be root for one test, so require_root is still exercised."""
    real = os.geteuid
    os.geteuid = lambda: 0
    case.addCleanup(setattr, os, "geteuid", real)


def patched(case, name, value):
    """Replace a module attribute for one test."""
    real = getattr(fan, name)
    setattr(fan, name, value)
    case.addCleanup(setattr, fan, name, real)


class SafeStateVerificationTests(unittest.TestCase):
    """safe_state() answers "are the fans demonstrably back?", not "did I try?"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def controller(self, channels=(3,), **kwargs):
        device = make_chip(self.tmp.name, channels, **kwargs)
        controller = fan.FanController(device)
        for channel in channels:
            controller.remember(channel)
        return controller

    def read(self, name):
        with open(os.path.join(self.tmp.name, "f71882fg.2592", name),
                  encoding="ascii") as fh:
            return fh.read().strip()

    def test_a_readback_of_automatic_mode_is_a_success(self):
        controller = self.controller(enable=1)
        with quiet():
            self.assertTrue(controller.safe_state([3], reason="test"))
        self.assertEqual(self.read("pwm3"), str(fan.SAFE_PWM))
        self.assertEqual(self.read("pwm3_enable"), str(fan.PWM_AUTOMATIC))

    def test_retained_manual_mode_is_a_failure(self):
        # The chip took the write and stayed in manual mode. Nothing in the
        # write path reports this; only the readback does.
        controller = self.controller(enable=1)
        with quiet() as out, registers(ignore=["pwm3_enable"]):
            self.assertFalse(controller.safe_state([3], reason="test"))
        self.assertEqual(self.read("pwm3_enable"), "1")
        self.assertIn("still in mode 1", out.getvalue())

    def test_an_unreadable_readback_is_a_failure(self):
        # An empty register parses to None. That used to be accepted next to
        # PWM_AUTOMATIC, so the channel most likely to be stuck at a stall
        # duty was the one reported as handed back.
        controller = self.controller(enable="")
        with quiet() as out, registers(ignore=["pwm3_enable"]):
            self.assertFalse(controller.safe_state([3], reason="test"))
        self.assertIn("unreadable", out.getvalue())

    def test_a_device_that_disappeared_is_a_failure(self):
        # The module was unloaded under a running service: the writes get
        # ENOENT and there is nothing left to read back.
        controller = fan.FanController(os.path.join(self.tmp.name, "vanished"))
        controller.remember(3)
        with quiet() as out:
            self.assertFalse(controller.safe_state([3], reason="test"))
        self.assertIn("unreadable", out.getvalue())

    def test_a_failed_write_is_a_failure_even_when_the_mode_reads_right(self):
        # The register already reads 2, so the readback alone would pass. It
        # is not evidence: the same interface just refused a write, and a
        # chip that rejects writes is not one to take reads from either.
        controller = self.controller(enable=2)
        with quiet() as out, registers(fail=["pwm3_enable"]):
            self.assertFalse(controller.safe_state([3], reason="test"))
        self.assertIn("did not accept the safe-state write", out.getvalue())

    def test_a_failed_duty_write_is_a_failure(self):
        controller = self.controller(enable=1)
        with quiet(), registers(fail=["pwm3"]):
            self.assertFalse(controller.safe_state([3], reason="test"))

    def test_no_channel_to_restore_is_a_failure_not_a_vacuous_success(self):
        controller = self.controller()
        with quiet() as out:
            self.assertFalse(controller.safe_state([], reason="test"))
        self.assertIn("no channel to restore", out.getvalue())

    def test_a_dry_run_reports_success_because_it_changed_nothing(self):
        device = make_chip(self.tmp.name, [3], enable=1)
        controller = fan.FanController(device, dry_run=True)
        controller.remember(3)
        with quiet():
            self.assertTrue(controller.safe_state([3], reason="test"))
        self.assertEqual(self.read("pwm3_enable"), "1")

    def test_every_channel_is_restored_even_when_an_earlier_one_fails(self):
        # Channel 1 is the first target, so giving up on the first failure
        # would leave 2 and 3 parked wherever the interrupted run had them.
        controller = self.controller(channels=(1, 2, 3), pwm=70, enable=1)
        with quiet() as out, registers(ignore=["pwm1_enable"]):
            self.assertFalse(controller.safe_state([1, 2, 3], reason="test"))
        self.assertEqual(self.read("pwm1_enable"), "1")
        for channel in (2, 3):
            self.assertEqual(self.read("pwm%d_enable" % channel),
                             str(fan.PWM_AUTOMATIC),
                             "channel %d was abandoned after channel 1 failed"
                             % channel)
            self.assertEqual(self.read("pwm%d" % channel), str(fan.SAFE_PWM))
        self.assertIn("1 of 3 channels", out.getvalue())

    def test_a_mixed_result_is_reported_per_channel(self):
        controller = self.controller(channels=(1, 2), enable={1: 1, 2: 1})
        with quiet() as out, registers(ignore=["pwm2_enable"]):
            self.assertFalse(controller.safe_state([1, 2], reason="test"))
        printed = out.getvalue()
        self.assertIn("channel 1 -> automatic mode (verified)", printed)
        self.assertIn("channel 2 is still in mode 1", printed)


class SafeStateCommandTests(unittest.TestCase):
    """`safe-state` exits nonzero unless every channel was proven back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        as_root(self)

    def run_safe_state(self, *extra):
        return fan.main(["safe-state", "--device-glob",
                         os.path.join(self.tmp.name, "f71882fg.*")] + list(extra))

    def test_zero_when_every_channel_verifies(self):
        make_chip(self.tmp.name, [1, 3], enable=1)
        with quiet() as out:
            self.assertEqual(self.run_safe_state(), 0)
        self.assertIn("automatic fan mode verified", out.getvalue())

    def test_nonzero_on_retained_manual_mode(self):
        make_chip(self.tmp.name, [3], enable=1)
        with quiet() as out, registers(ignore=["pwm3_enable"]):
            self.assertEqual(self.run_safe_state(), 1)
        self.assertNotIn("automatic fan mode verified", out.getvalue())

    def test_nonzero_on_an_unreadable_mode(self):
        make_chip(self.tmp.name, [3], enable="")
        with quiet(), registers(ignore=["pwm3_enable"]):
            self.assertEqual(self.run_safe_state(), 1)

    def test_nonzero_when_one_channel_of_several_fails(self):
        make_chip(self.tmp.name, [1, 2, 3], enable=1)
        with quiet(), registers(ignore=["pwm2_enable"]):
            self.assertEqual(self.run_safe_state(), 1)

    def test_nonzero_when_no_channel_is_controllable(self):
        # Tachometers but no duty registers: there is no way to hand these
        # fans back, so reporting that they were handed back is a lie.
        write_sysfs_tree(self.tmp.name, {"f71882fg.2592/fan3_input": "900\n"})
        with quiet() as out:
            self.assertEqual(self.run_safe_state(), 1)
        self.assertIn("no controllable channel", out.getvalue())

    def test_nonzero_when_the_chip_exposes_nothing_at_all(self):
        os.makedirs(os.path.join(self.tmp.name, "f71882fg.2592"))
        with quiet():
            self.assertEqual(self.run_safe_state(), 1)

    def test_a_dry_run_writes_nothing_and_is_not_a_verified_handback(self):
        # It used to exit 0 and print "automatic fan mode verified" after
        # writing nothing and reading nothing back. Every caller that gates a
        # deletion on `safe-state && rm` could have earned the rm with a flag.
        device = make_chip(self.tmp.name, [3], enable=1)
        with quiet() as out:
            code = self.run_safe_state("--dry-run")
        self.assertEqual(code, fan.EXIT_NOT_VERIFIED)
        self.assertNotEqual(code, 0)
        self.assertNotIn("verified", out.getvalue().lower())
        with open(os.path.join(device, "pwm3_enable"), encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), "1")


class RunShutdownTests(unittest.TestCase):
    """A stop that cannot hand the fans back is not a clean stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, [3], rpm=900, pwm=100, enable=1)
        self.cache = os.path.join(self.tmp.name, "state", "fan-calibration.json")
        fan.save_cache({"version": 1, "channels": [3],
                        "fans": {"3": {"minstop": 90, "minstart": 120,
                                       "min_rpm": 400, "max_rpm": 1500}}},
                       self.cache)
        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")
        as_root(self)
        patched(self, "find_temp_input", lambda sensor: self.sensor)
        # SIGTERM arrives on the second pass of the control loop.
        real_read = fan.read_temp
        seen = []

        def read_temp(path):
            seen.append(path)
            if len(seen) > 1:
                raise fan.Terminated("signal SIGTERM")
            return real_read(path)

        patched(self, "read_temp", read_temp)

    def run_loop(self):
        return fan.main(["run", "--device-glob",
                         os.path.join(self.tmp.name, "f71882fg.*"),
                         "--cache", self.cache, "--interval", "0"])

    def test_a_stop_that_restores_the_chip_exits_zero(self):
        with quiet():
            self.assertEqual(self.run_loop(), 0)
        with open(os.path.join(self.device, "pwm3_enable"), encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), str(fan.PWM_AUTOMATIC))

    def test_a_stop_that_cannot_restore_the_chip_exits_nonzero(self):
        # systemd reads the exit status. Reporting 0 here is what would let a
        # machine sit with its fan in manual mode and nothing complaining.
        with quiet() as out, registers(ignore=["pwm3_enable"]):
            self.assertEqual(self.run_loop(), 1)
        self.assertIn("could not be confirmed back", out.getvalue())


class TemperaturePlausibilityTests(unittest.TestCase):
    """The band the control loop and the calibration sweep both use."""

    def test_ordinary_readings_are_plausible(self):
        for value in (-49, 0, 20, 45, 90, 149):
            self.assertTrue(fan.temperature_is_plausible(value), value)

    def test_missing_readings_are_not(self):
        self.assertFalse(fan.temperature_is_plausible(None))

    def test_nan_and_the_infinities_are_not(self):
        # NaN compares false against every bound, so a bare range check would
        # pass it through as "not too hot".
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertFalse(fan.temperature_is_plausible(value), value)

    def test_values_outside_the_documented_band_are_not(self):
        for value in (fan.TEMP_PLAUSIBLE_MIN, fan.TEMP_PLAUSIBLE_MIN - 1,
                      fan.TEMP_PLAUSIBLE_MAX, fan.TEMP_PLAUSIBLE_MAX + 1,
                      -273, 900):
            self.assertFalse(fan.temperature_is_plausible(value), value)

    def test_unparseable_values_are_not(self):
        self.assertFalse(fan.temperature_is_plausible("warm"))
        self.assertFalse(fan.temperature_is_plausible(object()))

    def test_the_runtime_reader_uses_the_same_band(self):
        path = os.path.join(tempfile.mkdtemp(), "temp1_input")
        for celsius in (fan.TEMP_PLAUSIBLE_MAX + 1, fan.TEMP_PLAUSIBLE_MIN - 1):
            with open(path, "w", encoding="ascii") as fh:
                fh.write("%d\n" % (celsius * 1000))
            self.assertIsNone(fan.read_temp(path), celsius)


class CalibrationCase(unittest.TestCase):
    """A calibration sweep against a fake chip, with the wall clock removed.

    The tachometer answers the duty register, so the sweep converges where a
    real one would instead of running to the end of its range, and
    calibrate_channel is re-bound with settle times short enough for a test.

    With STALL_BELOW at 90 the sweep is deterministic, which is what lets a
    test aim at one step of it:

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
        self.write_temp(45)
        as_root(self)
        patched(self, "find_temp_input", lambda sensor: self.sensor)

        # A tachometer that answers the duty register, so the sweep converges
        # instead of running to the end of its range.
        real_rpm = fan.FanController.rpm

        def rpm(controller, channel):
            duty = controller.pwm(channel)
            return 0 if duty is None or duty < self.STALL_BELOW else 90 + duty * 6

        fan.FanController.rpm = rpm
        self.addCleanup(setattr, fan.FanController, "rpm", real_rpm)

        # Same measurement, without the wall-clock settle times.
        real_calibrate = fan.calibrate_channel
        patched(self, "calibrate_channel",
                lambda controller, channel, temp_path, settle=0.05, spin=0.05:
                real_calibrate(controller, channel, temp_path, settle, spin))

    def write_temp(self, celsius):
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("%d\n" % (celsius * 1000))

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


class CalibrationTelemetryTests(CalibrationCase):
    """A sweep that has lost the temperature may not take the next step down.

    The sweep lowers the duty until the fan stops. Losing the sensor half way
    through is the moment the procedure is most dangerous and least able to
    notice: every guard below it keys off a temperature that is no longer
    arriving.
    """

    def test_a_sensor_that_vanishes_mid_sweep_aborts_the_calibration(self):
        # The sweep is at duty 100 and turning when the hwmon node goes away.
        code, written, printed = self.calibrate(
            hook=lambda name, value:
            os.unlink(self.sensor) if (name, value) == ("pwm3", 100) else None)

        self.assertEqual(code, 1, printed)
        self.assertIn("stopped giving usable readings", printed)
        duties = self.duties(written)
        self.assertIn(100, duties, "the sweep never reached the trigger")
        self.assertNotIn(95, duties,
                         "the duty was lowered again after the sensor was lost")
        self.assertFalse([d for d in duties if d < 100 and d != fan.PWM_MIN],
                         "a lower duty was commanded blind: %s" % duties)

    def test_the_cleanup_still_runs_after_the_telemetry_is_lost(self):
        code, written, _ = self.calibrate(
            hook=lambda name, value:
            os.unlink(self.sensor) if (name, value) == ("pwm3", 100) else None)

        self.assertEqual(code, 1)
        self.assertEqual(written[-2:], [("pwm3", fan.SAFE_PWM),
                                        ("pwm3_enable", fan.PWM_AUTOMATIC)],
                         "the abort did not end in a safe state: %s" % written)
        with open(os.path.join(self.device, "pwm3_enable"), encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), str(fan.PWM_AUTOMATIC))

    def test_nothing_is_saved_when_the_sweep_aborts(self):
        self.calibrate(hook=lambda name, value:
                       os.unlink(self.sensor)
                       if (name, value) == ("pwm3", 100) else None)
        self.assertFalse(os.path.exists(self.cache))

    def test_an_unusable_reading_aborts_the_calibration(self):
        # A sysfs file cannot hold NaN - read_int parses integers - so these
        # are injected one level up, at the reader the sweep actually calls.
        for value in (None, float("nan"), float("inf"), float("-inf"),
                      fan.TEMP_PLAUSIBLE_MAX + 5, fan.TEMP_PLAUSIBLE_MIN - 5):
            with self.subTest(reading=value):
                samples = []

                def read_temp(path, value=value):
                    samples.append(path)
                    return 45 if len(samples) <= 2 else value

                patched(self, "read_temp", read_temp)
                if os.path.exists(self.cache):
                    os.unlink(self.cache)
                code, written, printed = self.calibrate()

                self.assertEqual(code, 1, printed)
                self.assertIn("stopped giving usable readings", printed)
                self.assertFalse(os.path.exists(self.cache))
                self.assertEqual(written[-1],
                                 ("pwm3_enable", fan.PWM_AUTOMATIC),
                                 "the abort did not end in a safe state")

    def test_a_hot_machine_still_aborts_on_the_temperature_it_read(self):
        # The plausibility check must not have swallowed the overheat abort:
        # CALIBRATION_ABORT_TEMP is well inside the plausible band.
        samples = []

        def read_temp(path):
            samples.append(path)
            return 45 if len(samples) <= 2 else fan.CALIBRATION_ABORT_TEMP + 1

        patched(self, "read_temp", read_temp)
        code, _, printed = self.calibrate()

        self.assertEqual(code, 1, printed)
        self.assertIn("temperature reached", printed)

    def test_a_complete_calibration_is_saved_and_exits_zero(self):
        code, written, printed = self.calibrate()

        self.assertEqual(code, 0, printed)
        self.assertTrue(os.path.exists(self.cache), printed)
        self.assertEqual(written[-1], ("pwm3_enable", fan.PWM_AUTOMATIC))
        cache = fan.load_cache(self.cache)
        self.assertEqual(cache["channels"], [3])

    def test_a_calibration_whose_cleanup_cannot_be_verified_is_not_a_success(self):
        # The measurements are fine; the chip would not take the fans back.
        # Saving the cache and printing "start the service when you are ready"
        # on a machine whose fan may still be parked at a stall duty is the
        # outcome this refuses.
        make_chip(self.tmp.name, [3], rpm=1400, pwm=160, enable=1)
        code, _, printed = self.calibrate(ignore=["pwm3_enable"])

        self.assertEqual(code, 1, printed)
        self.assertIn("NOT saved", printed)
        self.assertNotIn("Start the service when you are ready", printed)
        self.assertFalse(os.path.exists(self.cache),
                         "a calibration was saved after an unverified restore")


class CalibrationWriteGateTests(CalibrationCase):
    """Every control write in a calibration is a gate, not a suggestion.

    calibrate_channel used to call set_manual() and set_pwm() and throw the
    answer away. A chip that never left its own automatic curve, or that
    stopped acknowledging duties half way down, produced a full set of
    measurements, a saved cache and an exit status of 0 - describing a fan
    whose real duty nobody knew. Worse, the sweep's next step is always
    *lower*, so continuing past a refusal is stepping a fan towards its stall
    point blind.
    """

    def duties_after(self, written, duty):
        """Duties commanded after the first command of `duty`, in order."""
        seen = self.duties(written)
        return seen[seen.index(duty) + 1:] if duty in seen else []

    # -- the positive control ------------------------------------------------
    def test_a_calibration_whose_writes_are_all_accepted_still_completes(self):
        """The control for every test below.

        A gate that refuses everything is not a gate, it is a brick. This is
        the same chip as the failure cases, answering every command, and it
        has to produce a saved calibration and exit 0.
        """
        code, written, printed = self.calibrate()

        self.assertEqual(code, 0, printed)
        self.assertTrue(os.path.exists(self.cache), printed)
        self.assertIn("Start the service when you are ready", printed)
        # The sweep really did run: it visited the stall point and came back.
        duties = self.duties(written)
        self.assertIn(85, duties)
        self.assertIn(fan.PWM_MIN, duties)
        self.assertEqual(written[-1], ("pwm3_enable", fan.PWM_AUTOMATIC))

    # -- initial manual mode -------------------------------------------------
    def test_a_refused_manual_mode_command_aborts_before_any_duty(self):
        code, written, printed = self.calibrate(
            fail_when=nth_write("pwm3_enable", fan.PWM_MANUAL))

        self.assertEqual(code, 1, printed)
        self.assertIn("channel 3 is not in manual mode", printed)
        self.assertFalse(os.path.exists(self.cache))
        # Nothing was measured, so no duty was ever commanded. The safe state
        # writes full duty on the way out; that is the only pwm3 write here.
        self.assertEqual(self.duties(written), [fan.SAFE_PWM],
                         "a duty was commanded on a chip that never took the "
                         "manual-mode command: %s" % written)

    def test_a_chip_that_refuses_manual_mode_is_still_handed_back(self):
        code, written, _ = self.calibrate(
            fail_when=nth_write("pwm3_enable", fan.PWM_MANUAL))

        self.assertEqual(code, 1)
        self.assertEqual(written[-2:], [("pwm3", fan.SAFE_PWM),
                                        ("pwm3_enable", fan.PWM_AUTOMATIC)],
                         "the abort did not end in a safe state: %s" % written)

    # -- spin-up -------------------------------------------------------------
    def test_a_refused_full_duty_aborts_before_the_sweep(self):
        code, written, printed = self.calibrate(
            fail_when=nth_write("pwm3", fan.PWM_MAX))

        self.assertEqual(code, 1, printed)
        self.assertIn("full-speed duty", printed)
        self.assertFalse([d for d in self.duties_after(written, fan.PWM_MAX)
                          if d != fan.SAFE_PWM],
                         "the sweep started anyway: %s" % written)
        self.assertFalse(os.path.exists(self.cache))

    # -- the descent ---------------------------------------------------------
    def test_a_refused_sweep_duty_stops_the_descent(self):
        # Duty 100 is refused four steps into the descent. 95, 90 and 85 are
        # what the old code would have commanded next, each one lower than a
        # duty the chip never acknowledged.
        code, written, printed = self.calibrate(
            fail_when=nth_write("pwm3", 100))

        self.assertEqual(code, 1, printed)
        self.assertIn("descending-sweep duty 100", printed)
        lower = [d for d in self.duties_after(written, 100)
                 if d != fan.SAFE_PWM]
        self.assertEqual(lower, [],
                         "the duty was lowered after a refused command: %s"
                         % lower)
        self.assertFalse(os.path.exists(self.cache))

    # -- the zero duty -------------------------------------------------------
    def test_a_refused_zero_duty_aborts_before_the_restart_search(self):
        # This one commands a stop. Not knowing whether it landed is not
        # knowing whether the fan is stopped, which is what the stall cap and
        # the restart search both assume from here on.
        code, written, printed = self.calibrate(
            fail_when=nth_write("pwm3", fan.PWM_MIN))

        self.assertEqual(code, 1, printed)
        self.assertIn("zero duty", printed)
        self.assertFalse([d for d in self.duties_after(written, fan.PWM_MIN)
                          if d != fan.SAFE_PWM],
                         "the restart search ran anyway: %s" % written)

    # -- the restart search --------------------------------------------------
    def test_a_refused_restart_duty_aborts_the_restart_search(self):
        # Duty 80 only ever occurs on the way back up: the descent stops at
        # 85. The fan is stopped for this whole phase, so a refusal here is
        # the one that matters most.
        code, written, printed = self.calibrate(fail_when=nth_write("pwm3", 80))

        self.assertEqual(code, 1, printed)
        self.assertIn("restart duty 80", printed)
        self.assertFalse([d for d in self.duties_after(written, 80)
                          if d != fan.SAFE_PWM],
                         "the search kept climbing: %s" % written)
        self.assertFalse(os.path.exists(self.cache))

    # -- the final duties ----------------------------------------------------
    def test_a_refused_final_minimum_duty_aborts(self):
        # 100 is commanded twice: once in the descent, once as the measured
        # minimum at the end. The second one is this gate.
        code, _, printed = self.calibrate(
            fail_when=nth_write("pwm3", 100, index=2))

        self.assertEqual(code, 1, printed)
        self.assertIn("final-minimum duty 100", printed)
        self.assertFalse(os.path.exists(self.cache))

    def test_a_refused_cool_down_duty_aborts(self):
        code, _, printed = self.calibrate(
            fail_when=nth_write("pwm3", fan.PWM_MAX, index=2))

        self.assertEqual(code, 1, printed)
        self.assertIn("cool-down duty", printed)
        self.assertFalse(os.path.exists(self.cache))

    # -- what an abort must not do -------------------------------------------
    def test_a_refused_write_saves_nothing_and_recommends_nothing(self):
        code, _, printed = self.calibrate(fail_when=nth_write("pwm3", 100))

        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.cache),
                         "a calibration was saved from a sweep the chip "
                         "refused to take part in")
        self.assertNotIn("Start the service when you are ready", printed)
        self.assertNotIn("saved calibration", printed)

    def test_an_earlier_channel_is_discarded_when_a_later_one_is_refused(self):
        # Channel 1 measures cleanly and channel 3 is refused. Saving the
        # half of the sweep that happened to work would put a cache on the
        # machine describing a calibration run that aborted.
        make_chip(self.tmp.name, [1, 3], rpm=1400, pwm=160, enable=2)
        argv = ["calibrate", "--yes", "--channels", "1", "3", "--device-glob",
                os.path.join(self.tmp.name, "f71882fg.*"), "--cache", self.cache]
        with quiet() as out, registers(fail_when=nth_write("pwm3", 100)):
            code = fan.main(argv)

        self.assertEqual(code, 1, out.getvalue())
        self.assertFalse(os.path.exists(self.cache))

    def test_the_cleanup_still_runs_after_a_refused_write(self):
        code, written, _ = self.calibrate(fail_when=nth_write("pwm3", 100))

        self.assertEqual(code, 1)
        self.assertEqual(written[-2:], [("pwm3", fan.SAFE_PWM),
                                        ("pwm3_enable", fan.PWM_AUTOMATIC)])
        with open(os.path.join(self.device, "pwm3_enable"),
                  encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), str(fan.PWM_AUTOMATIC))

    def test_a_cleanup_that_also_fails_is_reported_not_swallowed(self):
        # The sweep was refused and so is the handback. The abort reason is
        # the sweep, but the operator has to be told the fans are not back -
        # that is the part that needs somebody to walk over to the machine.
        # The chip starts in manual mode, so an ignored pwm3_enable write
        # leaves it there and the readback says so.
        make_chip(self.tmp.name, [3], rpm=1400, pwm=160, enable=fan.PWM_MANUAL)
        code, _, printed = self.calibrate(ignore=["pwm3_enable"],
                                          fail_when=nth_write("pwm3", 100))

        self.assertEqual(code, 1, printed)
        self.assertIn("descending-sweep duty 100", printed)
        self.assertIn("could not be confirmed", printed)
        self.assertIn("NOT proven back", printed)


class RuntimeWriteAccountingTests(unittest.TestCase):
    """Refused writes are counted per channel and per register.

    The control loop used to keep one counter for the whole chip and reset it
    on any successful write. On a two-fan board that means the healthy fan
    zeroes the dead one's history every iteration: pwm1 could refuse every
    single command for the life of the service and never reach
    WRITE_FAILURE_LIMIT, because pwm3 answered right after it each time.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, [1, 3], rpm=900, pwm=100,
                                enable=1)
        self.cache = os.path.join(self.tmp.name, "state", "fan-calibration.json")
        fan.save_cache({"version": 1, "channels": [1, 3],
                        "fans": {c: {"minstop": 90, "minstart": 120,
                                     "min_rpm": 400, "max_rpm": 1500}
                                 for c in ("1", "3")}}, self.cache)
        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")
        as_root(self)
        patched(self, "find_temp_input", lambda sensor: self.sensor)
        self.stop_after(40)

    def stop_after(self, iterations):
        """End the loop by signal after N temperature reads, as a backstop.

        Every test here is supposed to end for its own reason; this only stops
        a regression from hanging the suite.
        """
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

    # -- the regression ------------------------------------------------------
    def test_one_dead_channel_is_not_masked_by_a_healthy_one(self):
        code, written, printed = self.run_loop(fail=["pwm1"])

        self.assertEqual(code, 1, printed)
        self.assertIn("pwm1 refused %d consecutive writes"
                      % fan.WRITE_FAILURE_LIMIT, printed)
        self.assertIn("not accepting commands", printed)
        # The healthy channel really was answering throughout, which is the
        # whole point: under the old counter that is what hid the failure.
        self.assertTrue([v for name, v in written if name == "pwm3"])

    def test_the_limit_triggers_at_exactly_the_documented_count(self):
        code, written, _ = self.run_loop(fail=["pwm1"])

        self.assertEqual(code, 1)
        # The sensor is pinned at 45 C, so the curve duty is not full duty and
        # the one SAFE_PWM write to pwm1 is the safe state on the way out.
        refused = [v for name, v in written if name == "pwm1"]
        self.assertEqual(refused[-1], fan.SAFE_PWM, refused)
        self.assertEqual(len(refused[:-1]), fan.WRITE_FAILURE_LIMIT,
                         "the loop ran on past the limit: %s" % refused)

    def test_a_channel_that_recovers_does_not_trip_the_limit(self):
        # Two refusals and then an answer. Intermittent is not dead, and a
        # service that exits on a transient EIO is a service that flaps.
        # Two refusals then an answer, twelve writes long, and healthy after
        # that - so the safe state on the way out is not itself refused and
        # the exit status is about the loop rather than about the cleanup.
        state = {"n": 0}

        def fail_when(name, _value):
            if name != "pwm1":
                return False
            state["n"] += 1
            return state["n"] <= 12 and state["n"] % 3 != 0

        code, written, printed = self.run_loop(fail_when=fail_when)

        self.assertEqual(code, 0, printed)
        self.assertNotIn("not accepting commands", printed)
        self.assertGreater(len([v for name, v in written if name == "pwm1"]),
                           fan.WRITE_FAILURE_LIMIT,
                           "the loop did not run long enough to prove this")

    # -- the initial manual-mode gate ---------------------------------------
    def test_a_refused_manual_mode_command_never_enters_the_loop(self):
        code, written, printed = self.run_loop(fail=["pwm1_enable"])

        self.assertEqual(code, 1, printed)
        self.assertIn("are not in manual mode", printed)
        self.assertIn("[1]", printed)
        # A curve was never driven: the only duty written is the safe state.
        duties = [v for name, v in written if name in ("pwm1", "pwm3")]
        self.assertEqual(set(duties), {fan.SAFE_PWM},
                         "the loop ran against a channel it does not own: %s"
                         % duties)

    def test_every_channel_is_attempted_before_the_manual_mode_verdict(self):
        # Returning at the first refusal would leave channel 3 unremembered,
        # and an unremembered channel is one the safe state does not restore.
        code, written, printed = self.run_loop(fail=["pwm1_enable"])

        self.assertEqual(code, 1)
        attempted = [name for name, value in written
                     if value == fan.PWM_MANUAL]
        self.assertEqual(attempted, ["pwm1_enable", "pwm3_enable"])
        # And the safe state was attempted on both, not just the one that
        # answered. pwm1_enable is the failing register here, so channel 1
        # cannot come back - but it has to be tried, and said out loud.
        handback = [name for name, value in written
                    if value == fan.PWM_AUTOMATIC]
        self.assertEqual(handback, ["pwm1_enable", "pwm3_enable"])
        self.assertIn("1 of 2 channels could not be confirmed", printed)
        with open(os.path.join(self.device, "pwm3_enable"),
                  encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), str(fan.PWM_AUTOMATIC),
                             "channel 3 was abandoned because channel 1 failed")

    def test_a_chip_that_accepts_manual_mode_runs_normally(self):
        # The control: the same loop, the same chip, nothing refused.
        code, _, printed = self.run_loop()

        self.assertEqual(code, 0, printed)
        self.assertNotIn("not in manual mode", printed)


class WriteAccountingUnitTests(unittest.TestCase):
    """The accounting itself, without a control loop around it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, [1, 3], enable=1)
        self.controller = fan.FanController(self.device)

    def test_a_success_clears_only_its_own_register(self):
        with quiet(), registers(fail=["pwm1"]):
            self.controller.set_pwm(1, 100)
            self.controller.set_pwm(1, 100)
        self.assertEqual(self.controller.failure_count(1, "pwm1"), 2)

        with quiet():
            self.controller.set_pwm(3, 100)          # a different channel
            self.controller.set_manual(1)            # the same channel, other
        self.assertEqual(self.controller.failure_count(1, "pwm1"), 2,
                         "an unrelated success cleared a real failure history")

        with quiet():
            self.controller.set_pwm(1, 100)
        self.assertEqual(self.controller.failure_count(1, "pwm1"), 0)

    def test_exhausted_writes_names_every_register_over_the_limit(self):
        with quiet(), registers(fail=["pwm1", "pwm3_enable"]):
            for _ in range(fan.WRITE_FAILURE_LIMIT):
                self.controller.set_pwm(1, 100)
                self.controller.set_manual(3)
        self.assertEqual(
            self.controller.exhausted_writes(fan.WRITE_FAILURE_LIMIT),
            [(1, "pwm1", fan.WRITE_FAILURE_LIMIT),
             (3, "pwm3_enable", fan.WRITE_FAILURE_LIMIT)])

    def test_nothing_is_exhausted_on_a_chip_that_answers(self):
        with quiet():
            for _ in range(fan.WRITE_FAILURE_LIMIT * 2):
                self.controller.set_pwm(1, 100)
                self.controller.set_pwm(3, 100)
        self.assertEqual(self.controller.exhausted_writes(), [])

    def test_a_dry_run_records_no_failure(self):
        controller = fan.FanController(os.path.join(self.tmp.name, "gone"),
                                       dry_run=True)
        with quiet():
            controller.set_pwm(3, 100)
        self.assertEqual(controller.exhausted_writes(1), [])

    def test_a_probe_the_chip_refuses_does_not_make_a_channel_active(self):
        # --probe spins up silent channels to find out whether a fan is
        # attached. A command the chip never took is not evidence that one is
        # not, and calibrating on that basis stalls a fan nobody measured.
        room = tempfile.TemporaryDirectory()
        self.addCleanup(room.cleanup)
        device = make_chip(room.name, [3], rpm=0, pwm=160, enable=2)
        controller = fan.FanController(device)
        with quiet() as out, registers(fail=["pwm3"]):
            self.assertEqual(controller.detect_active(probe=True, settle=0), [])
        self.assertIn("did not accept the probe", out.getvalue())

    def test_a_probe_the_chip_accepts_still_finds_a_spinning_fan(self):
        room = tempfile.TemporaryDirectory()
        self.addCleanup(room.cleanup)
        device = make_chip(room.name, [3], rpm=0, pwm=160, enable=2)
        controller = fan.FanController(device)

        def spin(name, _value):
            if name == "pwm3":
                with open(os.path.join(device, "fan3_input"), "w",
                          encoding="ascii") as fh:
                    fh.write("1100\n")

        with quiet(), registers(hook=spin):
            self.assertEqual(controller.detect_active(probe=True, settle=0), [3])


class SafeStateDryRunTests(unittest.TestCase):
    """A preview is not a hardware check, and must not be usable as one.

    `safe-state --dry-run` writes no register and reads none back. It used to
    print "automatic fan mode verified on channels [3]" and exit 0 anyway, so
    every caller that gates a deletion on the exit status - the installer, the
    uninstaller, the documented manual procedures - could have been handed the
    go-ahead by a flag that guarantees nothing happened.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        as_root(self)

    def run_safe_state(self, *extra):
        return fan.main(["safe-state", "--device-glob",
                         os.path.join(self.tmp.name, "f71882fg.*")]
                        + list(extra))

    def read(self, name):
        with open(os.path.join(self.tmp.name, "f71882fg.2592", name),
                  encoding="ascii") as fh:
            return fh.read().strip()

    def test_a_dry_run_over_a_chip_in_manual_mode_changes_nothing(self):
        # The chip is exactly where the failure cases leave it: manual mode,
        # parked at a low duty. A dry run must leave it there.
        make_chip(self.tmp.name, [3], pwm=70, enable=fan.PWM_MANUAL)
        with quiet(), registers() as written:
            code = self.run_safe_state("--dry-run")

        self.assertEqual(code, fan.EXIT_NOT_VERIFIED)
        self.assertEqual(written, [], "a dry run wrote to a register")
        self.assertEqual(self.read("pwm3_enable"), str(fan.PWM_MANUAL))
        self.assertEqual(self.read("pwm3"), "70")

    def test_a_dry_run_never_claims_a_verified_handback(self):
        make_chip(self.tmp.name, [3], pwm=70, enable=fan.PWM_MANUAL)
        with quiet() as out:
            self.run_safe_state("--dry-run")
        printed = out.getvalue().lower()

        self.assertNotIn("verified", printed)
        self.assertIn("nothing was proven", printed)

    def test_the_dry_run_status_is_neither_success_nor_the_failure_status(self):
        # Distinct on purpose. 0 means the fans are demonstrably back; 1 means
        # they are demonstrably not; this is neither, and a caller that treats
        # "not 0" as "do not delete" gets the right answer either way.
        make_chip(self.tmp.name, [3], enable=fan.PWM_MANUAL)
        with quiet():
            preview = self.run_safe_state("--dry-run")
        self.assertNotEqual(preview, 0)
        self.assertNotEqual(preview, 1)
        self.assertEqual(preview, fan.EXIT_NOT_VERIFIED)

    def test_the_same_chip_verifies_for_real_without_the_flag(self):
        # The control. Without --dry-run this chip is handed back and says so,
        # which is what makes the dry run's refusal a statement about the dry
        # run rather than about the chip.
        make_chip(self.tmp.name, [3], enable=fan.PWM_MANUAL)
        with quiet() as out:
            self.assertEqual(self.run_safe_state(), 0)
        self.assertIn("automatic fan mode verified", out.getvalue())
        self.assertEqual(self.read("pwm3_enable"), str(fan.PWM_AUTOMATIC))

    def test_a_dry_run_on_a_chip_with_nothing_controllable_is_still_not_zero(self):
        write_sysfs_tree(self.tmp.name, {"f71882fg.2592/fan3_input": "900\n"})
        with quiet():
            self.assertNotEqual(self.run_safe_state("--dry-run"), 0)


if __name__ == "__main__":
    unittest.main()
