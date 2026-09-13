"""A stop signal may not decide the exit status of a safe state.

safe_state() blocks SIGTERM, SIGINT, SIGHUP and SIGQUIT while it puts the
channels back, so the kernel holds any that arrive until the mask comes down
again. Restoring the mask is therefore the exact moment they are delivered,
and this program's handler turns each one into Terminated - a BaseException,
raised from inside signal.pthread_sigmask() itself.

That call used to sit in a `finally` in front of the aggregate verdict, so the
exception unwound past the `return False` underneath it and out through
main(), which reports a signalled stop as exit 0. The fan was still in manual
mode, and systemd, scripts/install.sh and scripts/uninstall.sh were all told
the handback had been proven.

The signals here are real ones, sent to this process with os.kill, because the
bug is about when the kernel chooses to deliver one; a stub cannot be wrong in
the same way. The handlers are installed and removed per test, so nothing
leaks into the rest of the suite. Nothing outside a disposable fake sysfs tree
is written, and no fan turns.
"""

import contextlib
import io
import os
import signal
import tempfile
import unittest

from helpers import load_fan, write_sysfs_tree

fan = load_fan()

# Two different signals, so the second one is genuinely a second pending
# signal: standard signals do not queue, and the same one sent twice while
# blocked arrives once.
SECOND_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


def make_chip(root, channels, rpm=1000, pwm=160, enable=1,
              name="f71882fg.2592"):
    """A fake platform device, in manual mode unless told otherwise.

    enable=1 by default: automatic mode has to be something this program puts
    there, not something the fixture started with, or a readback proves
    nothing.
    """
    spec = {}
    for channel in channels:
        spec["%s/fan%d_input" % (name, channel)] = "%d\n" % rpm
        spec["%s/pwm%d" % (name, channel)] = "%d\n" % pwm
        spec["%s/pwm%d_enable" % (name, channel)] = "%s\n" % enable
    write_sysfs_tree(root, spec)
    return os.path.join(root, name)


@contextlib.contextmanager
def chip(ignore=(), fail=(), send=(), after="pwm3", raise_on=None):
    """Stand in front of the module's register writes for one test.

    ignore    registers that accept a write and keep their old contents,
              which is what a chip that stays in manual mode does
    fail      registers whose write reports failure, as an EIO would
    send      signals to raise at this process once the safe state has
              started writing `after`. They are blocked at that point, so they
              stay pending in the kernel and are delivered by the unmasking -
              which is the bug's moment
    raise_on  raise Terminated from this register's write, once, while the
              safe state is running: a handler that fires between two
              bytecodes inside the restore loop, which is what happens to a
              signal that arrived just before the mask went up

    `send` and `raise_on` only fire inside safe_state(), because every command
    here writes the same registers on its way in as well.
    """
    real_write = fan.write_sysfs
    real_safe_state = fan.FanController.safe_state
    written = []
    inside = [False]
    raised = [False]

    def safe_state(controller, *args, **kwargs):
        inside[0] = True
        try:
            return real_safe_state(controller, *args, **kwargs)
        finally:
            inside[0] = False

    def write(path, value):
        name = os.path.basename(path)
        written.append((name, int(value)))
        if inside[0] and name == raise_on and not raised[0]:
            raised[0] = True
            raise fan.Terminated("signal SIGTERM")
        if inside[0] and send and name == after:
            for number in send:
                os.kill(os.getpid(), number)
        if name in fail:
            return False
        if name in ignore:
            return True
        return real_write(path, value)

    fan.write_sysfs = write
    fan.FanController.safe_state = safe_state
    try:
        yield written
    finally:
        fan.write_sysfs = real_write
        fan.FanController.safe_state = real_safe_state


class StopSignalCase(unittest.TestCase):
    """A fake chip, this process's own handlers, and an empty verdict record."""

    CHANNELS = (3,)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = make_chip(self.tmp.name, self.CHANNELS)
        self.glob = os.path.join(self.tmp.name, "f71882fg.*")
        real = os.geteuid
        os.geteuid = lambda: 0
        self.addCleanup(setattr, os, "geteuid", real)
        # Real handlers, removed again afterwards. Without them the SIGTERMs
        # below would kill the test runner instead of becoming Terminated;
        # left installed they would leak into every test that follows.
        for number in fan.STOP_SIGNALS:
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        fan.install_signal_handlers()
        # The record is process-wide and main() is what clears it, at the
        # start of a run. A test may not leave one behind for the next.
        self.addCleanup(fan.reset_handback_record)

    def mode(self, channel):
        with open(os.path.join(self.device, "pwm%d_enable" % channel),
                  encoding="ascii") as fh:
            return fh.read().strip()

    def drain_stop_signals(self):
        """Deliver any stop signal still pending, here and now.

        Each call to pthread_sigmask gives Python a chance to run a pending
        handler, so this turns a queue of them into a list of strings instead
        of leaving one to fire at an arbitrary point in the next test.
        """
        escaped = []
        for _ in range(len(fan.STOP_SIGNALS) + 1):
            try:
                signal.pthread_sigmask(signal.SIG_BLOCK, [])
            except fan.Terminated as exc:
                escaped.append(str(exc))
                continue
            break
        return escaped

    def run_main(self, argv):
        """fan.main(), asserting that it reports an exit status at all.

        main() owning the exit status is the contract every caller of this
        program is built on - systemd, scripts/install.sh, scripts/uninstall.sh
        and the documented manual procedures all read it. A Terminated that
        gets past main() is the failure these tests are about, so it is an
        assertion here rather than an exception that aborts the run.
        """
        try:
            code = fan.main(argv)
        except fan.Terminated as exc:
            escaped = [str(exc)] + self.drain_stop_signals()
            self.fail("a stop signal escaped main() (%s), so the command "
                      "never reported an exit status at all"
                      % ", ".join(escaped))
        self.assertEqual(self.drain_stop_signals(), [],
                         "a stop signal was left pending after the command "
                         "returned")
        return code

    def safe_state(self, **kwargs):
        with quiet() as out, chip(**kwargs) as written:
            code = self.run_main(["safe-state", "--device-glob", self.glob])
        return code, written, out.getvalue()


class SafeStateSecondSignalTests(StopSignalCase):
    """`safe-state` is the gate every removal of a fan binary is built on."""

    CHANNELS = (3, 5)

    def test_a_pending_signal_cannot_hide_a_total_restoration_failure(self):
        # Neither channel takes automatic mode, and two stop signals are
        # delivered by the unmasking. Exit 0 here is what let the installer
        # and the uninstaller delete the one program that could recover a fan
        # left in manual mode.
        code, _, printed = self.safe_state(
            ignore=["pwm3_enable", "pwm5_enable"], send=SECOND_SIGNALS)

        self.assertEqual(code, 1, printed)
        self.assertIn("2 of 2 channels could not be confirmed", printed)
        self.assertIn("does not turn an unverified handback into a clean stop",
                      printed)
        self.assertEqual(self.mode(3), "1")
        self.assertEqual(self.mode(5), "1")

    def test_a_pending_signal_cannot_hide_a_partial_restoration_failure(self):
        # One channel back, one not. A fan nobody is in charge of is a fan
        # nobody is in charge of, whatever the one next to it is doing.
        code, _, printed = self.safe_state(ignore=["pwm5_enable"],
                                           send=SECOND_SIGNALS)

        self.assertEqual(code, 1, printed)
        self.assertIn("1 of 2 channels could not be confirmed", printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC))
        self.assertEqual(self.mode(5), "1")

    def test_a_pending_signal_over_a_successful_restoration_is_a_clean_stop(self):
        # The handback is proven, so the signal may do what a stop signal
        # does. It must still be reported rather than swallowed.
        code, _, printed = self.safe_state(send=SECOND_SIGNALS)

        self.assertEqual(code, 0, printed)
        self.assertIn("automatic fan mode verified", printed)
        self.assertIn("stopped by signal", printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC))
        self.assertEqual(self.mode(5), str(fan.PWM_AUTOMATIC))
        self.assertFalse(fan.handback_unverified())

    def test_every_channel_is_restored_although_a_signal_is_pending(self):
        # The signals are raised while channel 3 is being written, i.e. before
        # channel 5 has been touched at all.
        code, written, printed = self.safe_state(send=SECOND_SIGNALS)

        self.assertEqual(code, 0, printed)
        self.assertIn(("pwm5_enable", fan.PWM_AUTOMATIC), written,
                      "the later channel was abandoned: %s" % written)

    def test_a_signal_raised_inside_the_restore_loop_is_retried_and_reported(self):
        # A signal that arrived just before the mask went up has already set
        # Python's pending flag, so its handler still fires inside the loop.
        # Abandoning the channel on it would park a fan on the strength of a
        # signal that changed nothing about the chip.
        code, written, printed = self.safe_state(raise_on="pwm3")

        self.assertEqual(code, 0, printed)
        self.assertIn("was interrupted by signal SIGTERM", printed)
        self.assertEqual(
            len([1 for name, _ in written if name == "pwm3"]), 2,
            "the interrupted channel was not tried again: %s" % written)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC))
        self.assertEqual(self.mode(5), str(fan.PWM_AUTOMATIC))

    def test_a_channel_interrupted_twice_is_not_claimed_as_restored(self):
        # Two interruptions in a row are taken as a refusal: the retry is
        # bounded, and an unprovable channel has to be reported as one.
        def always(path, value):
            raise fan.Terminated("signal SIGTERM")

        real = fan.write_sysfs
        fan.write_sysfs = always
        self.addCleanup(setattr, fan, "write_sysfs", real)
        with quiet() as out:
            code = self.run_main(["safe-state", "--device-glob", self.glob])

        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("2 of 2 channels could not be confirmed", out.getvalue())
        self.assertTrue(fan.handback_unverified())


class RunStopSignalTests(StopSignalCase):
    """The service loop: one signal stops it, a second lands in the cleanup."""

    def setUp(self):
        super(RunStopSignalTests, self).setUp()
        self.cache = os.path.join(self.tmp.name, "state", "fan-calibration.json")
        fan.save_cache({"version": 1, "channels": [3],
                        "fans": {"3": {"minstop": 90, "minstart": 120,
                                       "min_rpm": 400, "max_rpm": 1500}}},
                       self.cache)
        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")
        real_sensor = fan.find_temp_input
        fan.find_temp_input = lambda sensor: self.sensor
        self.addCleanup(setattr, fan, "find_temp_input", real_sensor)
        # The first stop signal, on the second pass of the control loop. The
        # second one is sent for real, from inside the safe state.
        real_read = fan.read_temp
        seen = []

        def read_temp(path):
            seen.append(path)
            if len(seen) > 1:
                raise fan.Terminated("signal SIGHUP")
            return real_read(path)

        fan.read_temp = read_temp
        self.addCleanup(setattr, fan, "read_temp", real_read)

    def run_loop(self, **kwargs):
        with quiet() as out, chip(**kwargs) as written:
            code = self.run_main(["run", "--device-glob", self.glob,
                                  "--cache", self.cache, "--interval", "0"])
        return code, written, out.getvalue()

    def test_a_second_signal_does_not_turn_a_failed_handback_into_exit_zero(self):
        code, _, printed = self.run_loop(ignore=["pwm3_enable"],
                                         send=SECOND_SIGNALS)

        self.assertEqual(code, 1, printed)
        self.assertIn("could not be confirmed back", printed)
        self.assertEqual(self.mode(3), "1",
                         "the fixture did not model a chip staying in manual")

    def test_a_second_signal_over_a_verified_handback_still_exits_zero(self):
        code, _, printed = self.run_loop(send=SECOND_SIGNALS)

        self.assertEqual(code, 0, printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC))
        self.assertFalse(fan.handback_unverified())

    def test_a_signal_delivered_after_the_verdict_still_exits_nonzero(self):
        # The verdict is recorded, not just returned, so that a Terminated
        # raised at any later bytecode - here from the lock release that
        # follows the safe state - cannot walk past it into main()'s
        # "stopped by signal" branch.
        real_release = fan.release_locks

        def release_locks(handles):
            real_release(handles)
            raise fan.Terminated("signal SIGTERM")

        fan.release_locks = release_locks
        self.addCleanup(setattr, fan, "release_locks", real_release)

        code, _, printed = self.run_loop(ignore=["pwm3_enable"])

        self.assertEqual(code, 1, printed)
        self.assertIn("were NOT confirmed back", printed)

    def test_the_same_late_signal_after_a_good_handback_is_a_clean_stop(self):
        # The control: the late signal is only fatal because the handback
        # failed, not because a late signal is fatal.
        real_release = fan.release_locks

        def release_locks(handles):
            real_release(handles)
            raise fan.Terminated("signal SIGTERM")

        fan.release_locks = release_locks
        self.addCleanup(setattr, fan, "release_locks", real_release)

        code, _, printed = self.run_loop()

        self.assertEqual(code, 0, printed)
        self.assertIn("stopped by signal SIGTERM", printed)


class CalibrateStopSignalTests(StopSignalCase):
    """The sweep parks a fan at duties it does not turn at; its cleanup is the
    only thing that takes it off them."""

    def setUp(self):
        super(CalibrateStopSignalTests, self).setUp()
        self.cache = os.path.join(self.tmp.name, "state", "fan-calibration.json")
        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")
        real_sensor = fan.find_temp_input
        fan.find_temp_input = lambda sensor: self.sensor
        self.addCleanup(setattr, fan, "find_temp_input", real_sensor)
        # The sweep itself is measured against a fake tachometer in
        # test_fan_safety.py; what matters here is that it wrote registers and
        # that the cleanup after it is the aggregate safe state. This stand-in
        # does the writing and nothing else, so the test stays about signals.
        real_calibrate = fan.calibrate_channel

        def calibrate_channel(controller, channel, temp_path, settle=0,
                              spin=0):
            controller.set_manual(channel)
            controller.set_pwm(channel, 90)
            return {"max_rpm": 1500, "min_rpm": 400, "stall_duty": 85,
                    "minstop": 95, "minstart": 120}

        fan.calibrate_channel = calibrate_channel
        self.addCleanup(setattr, fan, "calibrate_channel", real_calibrate)

    def calibrate(self, **kwargs):
        with quiet() as out, chip(**kwargs) as written:
            code = self.run_main(["calibrate", "--yes", "--channels", "3",
                                  "--device-glob", self.glob,
                                  "--cache", self.cache])
        return code, written, out.getvalue()

    def test_a_second_signal_does_not_turn_a_failed_cleanup_into_exit_zero(self):
        code, _, printed = self.calibrate(ignore=["pwm3_enable"],
                                          send=SECOND_SIGNALS)

        self.assertEqual(code, 1, printed)
        self.assertIn("NOT saved", printed)
        self.assertFalse(os.path.exists(self.cache),
                         "a calibration was saved after an unproven handback")
        self.assertNotIn("Start the service when you are ready", printed)

    def test_a_second_signal_over_a_verified_cleanup_still_exits_zero(self):
        code, _, printed = self.calibrate(send=SECOND_SIGNALS)

        self.assertEqual(code, 0, printed)
        self.assertTrue(os.path.exists(self.cache), printed)
        self.assertEqual(self.mode(3), str(fan.PWM_AUTOMATIC))


if __name__ == "__main__":
    unittest.main()
