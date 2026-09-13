"""A rollback needs its own quiescence and handback verdict, not the old one.

scripts/install.sh proves the fans are back under the controller before it
replaces or deletes a fan binary or unit. That verdict is earned at the top of
the run, and by the time a rollback happens it describes a machine that no
longer exists: install_files() has replaced the binary, enable_services() has
called restore_fan_activity(), and a fan service that was running before the
install is running again. Putting the previous files back - or removing the
ones this run installed - is the same act the gate on the way in refuses:
it takes away the executable an operator recovers a stalled fan with, and it
hands a live unit a different binary.

So the rollback stops every fan unit that could be running, proves no fan
writer of any name survived, and asks the chip again. If any of that fails it
touches no fan artifact at all, rolls back everything else, says which paths
it left alone, and still exits nonzero.

Every test runs scripts/install.sh for real against a fake root, a fake
systemctl and a fake /proc, and the "chip" is a disposable directory driven by
this repository's own fan program. Nothing under /sys, /etc or /usr/local is
read or written, and the assertions are on file contents and unit state, not
only on the command log.
"""

import os
import unittest

from fixtures import VALID_CACHE, ShellFixture
from helpers import REPO_ROOT

FAN_UNIT = "qnap-tsx70-fancontrol.service"
LCD_UNIT = "qnap-tsx70-lcd.service"
FAN_BIN = "usr/local/bin/qnap-tsx70-fancontrol"
LCD_BIN = "usr/local/bin/qnap-tsx70-lcd"
FAN_UNIT_PATH = "etc/systemd/system/qnap-tsx70-fancontrol.service"
# Written by --with-fan-control and not a way back from anything: a rollback
# that refuses to touch fan binaries still has to remove this.
MODULES_FILE = "etc/modules-load.d/qnap-tsx70.conf"

PREVIOUS_FAN = "#!/bin/sh\necho previous fan binary\n"
PREVIOUS_LCD = "#!/bin/sh\necho previous lcd binary\n"
PREVIOUS_FAN_UNIT = "[Unit]\nDescription=previous fan unit\n"


def repo_file(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as fh:
        return fh.read()


class RollbackFanGateCase(unittest.TestCase):
    """An install that gets as far as validation and then fails."""

    def setUp(self):
        self.fixture = ShellFixture(self)
        self.fixture.with_config()
        # A previous LCD install, active and holding the port, so the run is a
        # reinstall rather than a first install.
        self.fixture.write(os.path.join(self.fixture.bin_dir, "qnap-tsx70-lcd"),
                           PREVIOUS_LCD, 0o755)
        self.fixture.add_unit(LCD_UNIT, active=True, enabled=True,
                              mainpid=5201, procs=[5201])
        self.fixture.add_process(
            5201, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3", os.path.join(self.fixture.bin_dir,
                                          "qnap-tsx70-lcd")])
        self.fixture.hold_port(5201, unit=LCD_UNIT)
        # The LCD service comes back up but never reports active, so validate()
        # fails - after enable_services(), which is the whole point: that is
        # where restore_fan_activity() starts fan control again.
        self.fixture.make_start_inert(LCD_UNIT)

    def with_running_fan_control(self):
        """Fan control already installed, enabled, running, and calibrated."""
        self.fixture.write(
            os.path.join(self.fixture.bin_dir, "qnap-tsx70-fancontrol"),
            PREVIOUS_FAN, 0o755)
        self.fixture.add_unit(FAN_UNIT, active=True, enabled=True,
                              mainpid=6301, procs=[6301],
                              content=PREVIOUS_FAN_UNIT)
        self.fixture.add_process(
            6301, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3", os.path.join(self.fixture.bin_dir,
                                          "qnap-tsx70-fancontrol"), "run"])
        # Without a valid calibration restore_fan_activity() refuses to start
        # it again, and the writer this gate is about never comes back.
        self.fixture.write(os.path.join(self.fixture.state_dir,
                                        "fan-calibration.json"), VALID_CACHE)
        return self.fixture

    def install(self, *args):
        return self.fixture.install("--skip-deps", "--with-fan-control", *args)

    def safe_state_calls(self):
        return [call for call in self.fixture.fan_commands()
                if "safe-state" in call]

    def call_index(self, needle, last=False):
        calls = self.fixture.calls()
        hits = [i for i, call in enumerate(calls) if call == needle]
        self.assertTrue(hits, "%r never happened: %s" % (needle, calls))
        return hits[-1] if last else hits[0]

    def assertFanArtifactsUntouched(self, result):
        """The install's own files are still there, and rollback said so."""
        self.assertNotEqual(result.returncode, 0,
                            "an incomplete rollback reported success:\n%s"
                            % result.stdout)
        self.assertIn("INCOMPLETE ROLLBACK", result.stderr)
        self.assertNotIn("rollback complete", result.stderr,
                         "a rollback that left fan artifacts behind called "
                         "itself complete")
        self.assertEqual(self.fixture.read(FAN_BIN),
                         repo_file("bin/qnap-tsx70-fancontrol"),
                         "a fan binary was changed without a fresh handback")
        self.assertEqual(self.fixture.read(FAN_UNIT_PATH),
                         repo_file("systemd/qnap-tsx70-fancontrol.service"),
                         "a fan unit was changed without a fresh handback")

    def assertUnrelatedPathsRestored(self):
        """A refused fan gate is not permission to give up on everything else."""
        self.assertEqual(self.fixture.read(LCD_BIN), PREVIOUS_LCD,
                         "the LCD binary was not rolled back")
        self.assertFalse(self.fixture.exists(MODULES_FILE),
                         "a file this run created and that is not a fan "
                         "recovery artifact survived the rollback")


class FreshGateTests(RollbackFanGateCase):
    """The control: a healthy chip, and a rollback that may proceed."""

    def test_the_restarted_writer_is_stopped_and_the_fans_proven_again(self):
        self.with_running_fan_control()

        result = self.install()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        # enable_services() put fan control back up; the rollback took it down
        # again before touching the binary underneath it.
        restarted = self.call_index("restart %s" % FAN_UNIT)
        stopped = self.call_index("stop %s" % FAN_UNIT, last=True)
        self.assertGreater(stopped, restarted,
                           "the rollback replaced the binary of a running fan "
                           "service: %s" % self.fixture.calls())
        # Its own verdict, not the one from the top of the run.
        self.assertGreaterEqual(len(self.safe_state_calls()), 2,
                                "the rollback reused the pre-install handback: "
                                "%s" % self.fixture.fan_commands())
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "2")
        self.assertNotIn("INCOMPLETE ROLLBACK", result.stderr)

    def test_the_previous_fan_binary_and_unit_come_back(self):
        self.with_running_fan_control()

        result = self.install()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.read(FAN_BIN), PREVIOUS_FAN)
        self.assertEqual(self.fixture.read(FAN_UNIT_PATH), PREVIOUS_FAN_UNIT)
        self.assertEqual(self.fixture.read(LCD_BIN), PREVIOUS_LCD)

    def test_the_fan_service_is_started_again_only_after_its_files_are_back(self):
        self.with_running_fan_control()

        result = self.install()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.fixture.unit_state(FAN_UNIT)["active"],
                        "a service that was running before the install was "
                        "left down by the rollback")
        # The last restart is the rollback's, and by then the previous binary
        # is on disk: this asserts the file, not the order of the log.
        self.assertEqual(self.fixture.read(FAN_BIN), PREVIOUS_FAN)
        self.assertGreater(self.call_index("restart %s" % FAN_UNIT, last=True),
                           self.call_index("stop %s" % FAN_UNIT, last=True))


class RefusedQuiescenceTests(RollbackFanGateCase):
    """The chip is healthy; something is still driving it."""

    def test_a_fan_unit_that_will_not_stop_stops_the_rollback(self):
        self.with_running_fan_control()
        # The pre-install stop works, so the run gets as far as validation and
        # FAN_HANDBACK_VERIFIED is set. The rollback's stop is the one that
        # fails, which is exactly the stale-verdict case.
        self.fixture.fail_verb("stop", FAN_UNIT, after=1)

        result = self.install()

        self.assertFanArtifactsUntouched(result)
        self.assertUnrelatedPathsRestored()
        self.assertIn("could not be stopped", result.stderr)

    def test_a_stop_that_leaves_the_unit_active_stops_the_rollback(self):
        self.with_running_fan_control()
        # Not the transition's stop - that one works, and the run gets as far
        # as validation - but the one the rollback makes.
        self.fixture.make_stop_ineffective(FAN_UNIT, after=1)

        result = self.install()

        self.assertFanArtifactsUntouched(result)
        self.assertUnrelatedPathsRestored()
        self.assertIn("still active", result.stderr)

    def test_a_surviving_fan_writer_stops_the_rollback(self):
        self.with_running_fan_control()
        # A writer that appears at the daemon-reload in enable_services(),
        # after the pre-install quiescence proof, and that no stop can remove.
        self.fixture.spawn_process_on_daemon_reload(
            7711, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3",
                  os.path.join(self.fixture.bin_dir, "qnap-tsx70-fancontrol"),
                  "run"])

        result = self.install()

        self.assertFanArtifactsUntouched(result)
        self.assertUnrelatedPathsRestored()
        self.assertIn("still running", result.stderr)
        self.assertTrue(self.fixture.process_exists(7711),
                        "the installer signalled a process it only observed")

    def test_an_incomplete_rollback_does_not_start_the_fan_service_again(self):
        # Starting it again would run it against the binary and unit file the
        # failed install wrote, which are the ones that were not rolled back.
        self.with_running_fan_control()
        self.fixture.fail_verb("stop", FAN_UNIT, after=1)

        result = self.install()

        self.assertFanArtifactsUntouched(result)
        calls = self.fixture.calls()
        last_stop = max(index for index, call in enumerate(calls)
                        if call == "stop %s" % FAN_UNIT)
        self.assertEqual(
            [call for call in calls[last_stop:]
             if call in ("restart %s" % FAN_UNIT, "enable %s" % FAN_UNIT)], [],
            "the fan service was started again onto files that were never "
            "rolled back: %s" % calls)
        self.assertIn("not enabling or starting", result.stderr)

    def test_the_stale_pre_install_verdict_is_not_reused(self):
        # Belt and braces over the case above: the run did prove the handback
        # once, and the log has to show the rollback refusing anyway.
        self.with_running_fan_control()
        self.fixture.make_stop_ineffective(FAN_UNIT, after=1)

        result = self.install()

        self.assertIn("automatic fan mode verified", result.stdout)
        self.assertIn("proving", result.stderr)
        self.assertFanArtifactsUntouched(result)


class RefusedHandbackTests(RollbackFanGateCase):
    """Nothing is writing; the chip will not say the fans are back.

    These are first-time `--with-fan-control` installs: there is no fan binary
    or unit on the machine, so the gate on the way in has nothing to protect
    and never runs. The rollback is the one that would take the newly
    installed recovery program away again, and on a chip that may have a fan
    parked in manual mode that is the worst possible moment for it.
    """

    def test_a_chip_that_stays_in_manual_mode_keeps_the_new_fan_binary(self):
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.install()

        self.assertFanArtifactsUntouched(result)
        self.assertUnrelatedPathsRestored()
        self.assertIn("still in mode 1", result.stderr)
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "1")

    def test_a_refused_register_write_keeps_the_new_fan_binary(self):
        self.fixture.with_fan_controller(enable=1,
                                         fail_writes=["pwm3_enable"])

        result = self.install()

        self.assertFanArtifactsUntouched(result)
        self.assertUnrelatedPathsRestored()
        self.assertIn("did not accept the safe-state write", result.stderr)

    def test_a_controller_that_is_not_there_keeps_the_new_fan_binary(self):
        # The driver is not loaded, so there is nothing to ask - and that is
        # not permission to remove the program that would have asked. --force
        # is what gets past preflight on a host with no controller; it has
        # never meant "and skip the handback".
        self.fixture.without_fan_controller()

        result = self.install("--force")

        self.assertFanArtifactsUntouched(result)
        self.assertUnrelatedPathsRestored()

    def test_an_unreadable_mode_register_keeps_the_new_fan_binary(self):
        # A register that reads back as nothing at all is not a pass: it is
        # the case where a fan is most likely to be parked with nobody in
        # charge of it.
        self.fixture.with_fan_controller(enable="",
                                         ignore_writes=["pwm3_enable"])

        result = self.install()

        self.assertFanArtifactsUntouched(result)
        self.assertUnrelatedPathsRestored()

    def test_the_skipped_paths_are_recorded_where_they_can_be_found(self):
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.install()

        self.assertNotEqual(result.returncode, 0)
        backups = self.fixture.backups()
        self.assertEqual(len(backups), 1, backups)
        record = os.path.join(backups[0], "txn", "fan-rollback-skipped")
        self.assertTrue(os.path.exists(record),
                        "the incomplete rollback left no record behind")
        with open(record, encoding="utf-8") as fh:
            skipped = fh.read()
        self.assertIn("qnap-tsx70-fancontrol", skipped)
        self.assertIn(record, result.stderr)

    def test_an_incomplete_rollback_does_not_restart_the_fan_unit(self):
        # Starting a fan service onto the binary and unit file the failed
        # install wrote is not a rollback, and the fans were never proven
        # back.
        self.with_running_fan_control()
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])
        # The pre-install gate would refuse this chip outright, so the run
        # never reaches enable_services. Nothing about a fan artifact may be
        # changed on the way out either.
        result = self.install()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.read(FAN_BIN), PREVIOUS_FAN,
                         "the fan binary was replaced although the handback "
                         "was never proven")
        self.assertEqual(self.fixture.read(FAN_UNIT_PATH), PREVIOUS_FAN_UNIT)


class NoFanArtifactTests(RollbackFanGateCase):
    """A transaction with no fan artifact in it reaches no gate at all."""

    def test_an_lcd_only_rollback_never_writes_to_the_controller(self):
        # The rule on the way in - a run that is not touching a fan recovery
        # artifact has no business writing to a fan controller - holds on the
        # way out too.
        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.fan_commands(), [],
                         "an LCD-only rollback wrote to the fan controller")
        self.assertEqual(self.fixture.read(LCD_BIN), PREVIOUS_LCD)
        self.assertNotIn("INCOMPLETE ROLLBACK", result.stderr)


if __name__ == "__main__":
    unittest.main()
