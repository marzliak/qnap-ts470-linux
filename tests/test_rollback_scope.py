"""A rollback undoes what this run changed, not everything it wrote down.

txn_snapshot_all() records every path scripts/install.sh is capable of
touching, on every run: both binaries, both units, the config, the modules
file, the cache, and the whole legacy tree. That is correct for a manifest -
rollback cannot guess what was there before - and it was wrong as an answer to
"is fan control part of this rollback?".

Reading the manifest made an LCD-only install on a machine that happens to
have fan control look exactly like a fan install. A failure anywhere in it
then:

  * stopped a fan service this run had never touched, in order to satisfy a
    handback gate the run had no business reaching;
  * scanned the process table for fan writers, and refused the whole rollback
    if one was found - so an unrelated LCD failure could not be undone because
    fan control was doing its job;
  * ran safe-state against a controller this run had not written to, which on
    a chip mid-calibration is a second writer;
  * "restored" a fan binary and unit file from bytes identical to the ones
    already on disk, taking the recovery executable away for the duration;
  * and restarted the fan service afterwards, because step 4 restarts every
    unit that was active at snapshot time.

The question is narrower: did *this* transaction write, replace, delete, stop,
start, enable or disable a fan artifact? These tests hold both halves of that
line - the LCD-only case where the answer is no and nothing of fan control may
be reached, and the controls where the answer is yes and the full gate must
still run.

Everything runs scripts/install.sh for real against a fake root, a fake
systemctl and a fake /proc. The chip is a disposable directory driven by this
repository's own fan program; nothing under /sys, /etc or /usr/local is read
or written.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import VALID_CACHE, ShellFixture
from helpers import REPO_ROOT

FAN_UNIT = "qnap-tsx70-fancontrol.service"
LCD_UNIT = "qnap-tsx70-lcd.service"
FAN_BIN = "usr/local/bin/qnap-tsx70-fancontrol"
LCD_BIN = "usr/local/bin/qnap-tsx70-lcd"
FAN_UNIT_PATH = "etc/systemd/system/qnap-tsx70-fancontrol.service"

PREVIOUS_FAN = "#!/bin/sh\necho previous fan binary\n"
PREVIOUS_LCD = "#!/bin/sh\necho previous lcd binary\n"
PREVIOUS_FAN_UNIT = "[Unit]\nDescription=previous fan unit\n"

FAN_WRITER_PID = 6301


def repo_file(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as fh:
        return fh.read()


class ScopeCase(unittest.TestCase):
    """A machine running both services, on a chip that will not hand back."""

    def setUp(self):
        self.fixture = ShellFixture(self)
        self.fixture.with_config()
        self.fixture.write(os.path.join(self.fixture.bin_dir,
                                        "qnap-tsx70-lcd"), PREVIOUS_LCD, 0o755)
        self.fixture.add_unit(LCD_UNIT, active=True, enabled=True,
                              mainpid=5201, procs=[5201])
        self.fixture.add_process(
            5201, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3", os.path.join(self.fixture.bin_dir,
                                          "qnap-tsx70-lcd")])
        self.fixture.hold_port(5201, unit=LCD_UNIT)

    def fail_at_validation(self):
        """The LCD service starts and never reports active.

        validate() then fails and the rollback runs. It is the only thing
        wrong with these runs, and it is deliberately nothing to do with fan
        control: that is what makes "the rollback reached fan control" a
        finding rather than a consequence.
        """
        self.fixture.make_start_inert(LCD_UNIT)

    def with_fan_control(self, broken=True):
        """Fan control installed, enabled, running, and mid-manual-mode.

        The chip is in manual mode and its mode register swallows writes, so
        any handback attempted against it fails. That is deliberate: it is the
        state in which touching fan control out of scope does the most damage,
        and it makes "the gate was never reached" distinguishable from "the
        gate was reached and happened to pass".
        """
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      PREVIOUS_FAN, 0o755)
        fixture.add_unit(FAN_UNIT, active=True, enabled=True,
                         mainpid=FAN_WRITER_PID, procs=[FAN_WRITER_PID],
                         content=PREVIOUS_FAN_UNIT)
        fixture.add_process(
            FAN_WRITER_PID, "python3", exe=fixture.python_stub_versioned,
            argv=["python3",
                  os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                  "run"])
        fixture.write(os.path.join(fixture.state_dir, "fan-calibration.json"),
                      VALID_CACHE)
        if broken:
            fixture.with_fan_controller(enable=1,
                                        ignore_writes=["pwm3_enable"])
        else:
            fixture.with_fan_controller(enable=1)
        return fixture

    def fan_unit_calls(self):
        return [call for call in self.fixture.calls()
                if call.endswith(FAN_UNIT)
                and call.split()[0] in ("stop", "start", "restart", "enable",
                                        "disable")]


class LcdOnlyRollbackTests(ScopeCase):
    """The answer is no, so fan control is not reached at all."""

    def failed_lcd_only_install(self):
        self.with_fan_control()
        self.fail_at_validation()
        result = self.fixture.install("--skip-deps")
        self.assertNotEqual(result.returncode, 0,
                            "the install was supposed to fail:\n%s"
                            % result.stdout)
        return result

    def test_no_fan_service_is_stopped_started_or_re_enabled(self):
        self.failed_lcd_only_install()

        self.assertEqual(self.fan_unit_calls(), [],
                         "an LCD-only rollback changed what a fan unit was "
                         "doing: %s" % self.fixture.calls())

    def test_the_fan_service_is_still_active_and_enabled(self):
        self.failed_lcd_only_install()

        state = self.fixture.unit_state(FAN_UNIT)
        self.assertTrue(state["active"], "fan control was left down")
        self.assertTrue(state["enabled"], "fan control was left disabled")

    def test_the_fan_writer_is_neither_scanned_for_nor_signalled(self):
        result = self.failed_lcd_only_install()

        self.assertTrue(self.fixture.process_exists(FAN_WRITER_PID),
                        "the fan writer was killed by an LCD-only rollback")
        self.assertEqual(self.fixture.kill_calls(), [])
        self.assertNotIn("no fan control process remains", result.stdout)
        self.assertNotIn("fan control still running", result.stderr)

    def test_the_controller_is_never_written_to(self):
        self.with_fan_control()
        self.fail_at_validation()
        before = (self.fixture.fan_register("pwm3_enable"),
                  self.fixture.fan_register("pwm3"))

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.fan_commands(), [],
                         "an LCD-only rollback invoked the fan program")
        self.assertEqual((self.fixture.fan_register("pwm3_enable"),
                          self.fixture.fan_register("pwm3")), before,
                         "a register was written by a run with no fan "
                         "artifact in scope")
        self.assertEqual(before[0], "1", "the chip was not in manual mode, so "
                                         "this proves less than it should")
        self.assertNotIn("automatic fan mode", result.stdout)

    def test_the_fan_binary_and_unit_are_byte_for_byte_unchanged(self):
        self.failed_lcd_only_install()

        self.assertEqual(self.fixture.read(FAN_BIN), PREVIOUS_FAN)
        self.assertEqual(self.fixture.read(FAN_UNIT_PATH), PREVIOUS_FAN_UNIT)

    def test_the_fan_binary_is_not_even_replaced_with_identical_bytes(self):
        # Restoring a file the run never changed writes the same bytes, so
        # content alone cannot see it. The file is still replaced, though:
        # `install` creates a new one. For the executable an operator recovers
        # a stalled fan with, "briefly not there, then there again with the
        # same contents" is not the same as "untouched", and it is the only
        # difference an out-of-scope restore leaves behind.
        self.with_fan_control()
        self.fail_at_validation()
        binary = os.path.join(self.fixture.root, FAN_BIN)
        unit = os.path.join(self.fixture.root, FAN_UNIT_PATH)
        before = (os.stat(binary).st_ino, os.stat(unit).st_ino)

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual((os.stat(binary).st_ino, os.stat(unit).st_ino),
                         before,
                         "a fan artifact this run never changed was replaced "
                         "by the rollback")

    def test_the_rollback_says_fan_control_was_not_part_of_it(self):
        result = self.failed_lcd_only_install()

        self.assertIn("this transaction changed no fan binary or unit",
                      result.stdout)
        self.assertNotIn("INCOMPLETE ROLLBACK", result.stderr)
        self.assertNotIn("proving", result.stderr)

    def test_everything_that_was_in_scope_is_still_rolled_back(self):
        # A rollback that leaves fan control alone is not a rollback that gave
        # up: the LCD binary this run replaced comes back.
        self.failed_lcd_only_install()

        self.assertEqual(self.fixture.read(LCD_BIN), PREVIOUS_LCD)

    def test_a_healthy_chip_is_left_alone_just_the_same(self):
        # The gate is skipped because nothing of fan control was changed, not
        # because the chip would have refused. Same run, chip that works.
        self.with_fan_control(broken=False)
        self.fail_at_validation()

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.fan_commands(), [])
        self.assertEqual(self.fan_unit_calls(), [])
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "1")

    def test_a_machine_with_no_fan_control_at_all_is_unaffected(self):
        # The original shape of this case, kept as a floor: nothing to touch,
        # nothing touched.
        self.fail_at_validation()

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.fan_commands(), [])
        self.assertEqual(self.fixture.read(LCD_BIN), PREVIOUS_LCD)
        self.assertNotIn("INCOMPLETE ROLLBACK", result.stderr)


class InScopeRollbackControlTests(ScopeCase):
    """The answer is yes, so the full gate still runs.

    Without these the narrowing above would be indistinguishable from deleting
    the gate.
    """

    def test_stopping_the_fan_service_puts_the_gate_back_in_scope(self):
        # --with-fan-control stops the running fan service so its binary can be
        # replaced, and step 4 of the rollback would start it again. That is
        # fan control stopped and started by this transaction, so the gate
        # runs - and on this chip it refuses, and the service is left down
        # rather than restarted onto an unproven machine.
        self.with_fan_control()
        self.fail_at_validation()

        result = self.fixture.install("--skip-deps", "--with-fan-control")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("this transaction changed no fan binary or unit",
                         result.stdout)
        self.assertIn("proving", result.stderr)
        self.assertIn("still in mode 1", result.stderr)
        self.assertIn("not enabling or starting %s" % FAN_UNIT, result.stderr)

    def test_a_replaced_fan_binary_is_gated_and_left_as_installed(self):
        # A first --with-fan-control install: there is no fan binary on the
        # machine, so the gate on the way in has nothing to protect and never
        # runs. install_files() then writes one, which makes the rollback the
        # thing that would take it away again.
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])
        self.fail_at_validation()

        result = self.fixture.install("--skip-deps", "--with-fan-control")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("INCOMPLETE ROLLBACK", result.stderr)
        self.assertIn("still in mode 1", result.stderr)
        self.assertEqual(self.fixture.read(FAN_BIN),
                         repo_file("bin/qnap-tsx70-fancontrol"),
                         "the newly installed fan binary was taken away on a "
                         "machine whose fans were never proven back")
        self.assertEqual(self.fixture.read(FAN_UNIT_PATH),
                         repo_file("systemd/qnap-tsx70-fancontrol.service"))

    def test_with_fan_control_on_a_healthy_chip_restores_and_restarts(self):
        self.with_fan_control(broken=False)
        self.fail_at_validation()

        result = self.fixture.install("--skip-deps", "--with-fan-control")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("INCOMPLETE ROLLBACK", result.stderr)
        self.assertEqual(self.fixture.read(FAN_BIN), PREVIOUS_FAN)
        self.assertEqual(self.fixture.read(FAN_UNIT_PATH), PREVIOUS_FAN_UNIT)
        self.assertTrue(self.fixture.unit_state(FAN_UNIT)["active"],
                        "a fan service this run stopped was left down")
        self.assertGreaterEqual(
            len([c for c in self.fixture.fan_commands() if "safe-state" in c]),
            2, "the rollback reused the pre-install verdict: %s"
               % self.fixture.fan_commands())

    def test_a_stopped_legacy_fan_service_puts_the_gate_back_in_scope(self):
        # No --with-fan-control, so no fan binary is written. The migration
        # still stops the legacy fan service, and step 4 of the rollback would
        # start it again - which is fan control stopped and started by this
        # transaction, and so is gated.
        fixture = self.fixture
        fixture.with_legacy()
        fixture.units["saturn-fancontrol.service"]["active"] = True
        fixture._save_units()  # noqa: SLF001 - the fixture's own state file
        fixture.with_fan_controller(enable=1, ignore_writes=["pwm3_enable"])
        self.fail_at_validation()

        result = fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("this transaction changed no fan binary or unit",
                         result.stdout)
        self.assertIn("proving", result.stderr)
        self.assertIn("still in mode 1", result.stderr)

    def test_deleting_legacy_fan_artifacts_is_gated(self):
        # The migration gets all the way to the legacy cleanup, deletes
        # saturn-fancontrol, and then the cleanup's daemon-reload fails. The
        # rollback would put that binary back, so the gate runs.
        fixture = self.fixture
        fixture.with_legacy()
        fixture.with_fan_controller(enable=1)
        # enable_services() does the first daemon-reload; remove_legacy() does
        # the second, after the deletions.
        fixture.fail_verb("daemon-reload", "", after=1)

        result = fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("this transaction changed no fan binary or unit",
                         result.stdout)
        self.assertGreaterEqual(
            len([c for c in fixture.fan_commands() if "safe-state" in c]), 2,
            "the rollback did not prove the handback again: %s"
            % fixture.fan_commands())
        self.assertTrue(fixture.exists("usr/local/bin/saturn-fancontrol"),
                        "the legacy fan binary was not restored")


if __name__ == "__main__":
    unittest.main()
