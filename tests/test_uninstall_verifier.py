"""The uninstaller's handback gate runs this repository's verifier.

Everything past the gate in scripts/uninstall.sh is a `rm`. The gate's entire
value is the exit status of `safe-state`, and that status used to come from
`$BIN_DIR/qnap-tsx70-fancontrol` - the binary the run is about to delete.

That is the subject of a removal authorising its own removal, and it fails in
both directions:

  * an older installed release verifies less than this candidate does. A 1.x
    binary whose safe-state accepted an unreadable `pwmN_enable` as automatic
    mode exits 0 on exactly the chip this gate exists to catch;
  * a binary that has been replaced - by an operator's edit, a broken package,
    or anything else - can exit 0 having written no register at all. There is
    no way for the uninstaller to tell that apart from a real handback, and it
    then deletes the unit and the binary that were the way back.

So the gate runs the reviewed copy from the repository the uninstaller was
started from, resolved the way install.sh resolves it, and validates that it
can actually be executed before anything is mutated. A verifier that cannot
run is not a verdict of "safe" or "unsafe"; it is no verdict, and the only
correct response to no verdict is to remove nothing.

The chip here is a disposable directory and the program driving it is the real
bin/qnap-tsx70-fancontrol, so what these tests gate on is what safe-state
actually verifies rather than a stub that agrees with them.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import ShellFixture
from helpers import REPO_ROOT

LYING_BINARY = "#!/bin/sh\n# exits 0 and writes no register at all\nexit 0\n"


class VerifierCase(unittest.TestCase):
    def setUp(self):
        self.fixture = ShellFixture(self)

    def installed(self, fan_active=False, **chip):
        """A machine with both services installed and a described chip."""
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit("qnap-tsx70-lcd.service", active=True, enabled=True)
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=fan_active,
                         enabled=True)
        fixture.with_fan_chip(**chip)
        return fixture

    def assertKeptEverything(self, result, fixture):
        self.assertNotEqual(result.returncode, 0,
                            "the uninstaller should have refused:\n%s"
                            % result.stdout)
        self.assertTrue(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"),
                        "the fan binary was deleted after an unproven handback")
        self.assertTrue(
            fixture.exists("etc/systemd/system/qnap-tsx70-fancontrol.service"),
            "the fan unit was deleted after an unproven handback")
        self.assertIn("nothing was removed", result.stderr)


class LyingInstalledBinaryTests(VerifierCase):
    """An installed binary that exits 0 without touching a register."""

    def test_a_false_success_from_the_installed_binary_is_not_believed(self):
        # The chip is in manual mode and its mode register swallows writes, so
        # the honest answer is "not handed back". The installed binary says
        # otherwise, loudly and cheaply. The run must refuse.
        fixture = self.installed(fan_active=True, enable=1,
                                 ignore_writes=["pwm3_enable"])
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      LYING_BINARY, 0o755)

        result = fixture.uninstall()

        self.assertKeptEverything(result, fixture)
        self.assertEqual(fixture.fan_register("pwm3_enable"), "1",
                         "the chip left manual mode, so this proves nothing")

    def test_the_repository_verifier_is_what_ran(self):
        fixture = self.installed(fan_active=True, enable=1,
                                 ignore_writes=["pwm3_enable"])
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      LYING_BINARY, 0o755)

        result = fixture.uninstall()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("safe-state", " ".join(fixture.fan_commands()),
                      "the repository verifier was never invoked")
        self.assertIn("still in mode 1", result.stderr,
                      "the refusal did not come from a real readback")

    def test_the_installed_binary_is_never_executed(self):
        # A stub that records the fact that it ran. The gate must not touch
        # it: what gets deleted does not get a vote on the deletion.
        fixture = self.installed(fan_active=True, enable=1,
                                 ignore_writes=["pwm3_enable"])
        marker = os.path.join(fixture.harness, "installed-binary-ran")
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\n: > %s\nexit 0\n" % marker, 0o755)

        fixture.uninstall()

        self.assertFalse(os.path.exists(marker),
                         "the uninstaller ran the binary it was about to delete")

    def test_a_lying_binary_on_a_healthy_chip_still_removes(self):
        # The control. The same lying stub, a chip that does take the fans
        # back, and the repository verifier says so - so the removal is earned
        # by the chip's readback and not by the stub's exit status.
        fixture = self.installed(fan_active=True, enable=1)
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      LYING_BINARY, 0o755)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertEqual(fixture.fan_register("pwm3_enable"), "2")
        self.assertFalse(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertIn("automatic mode (verified)", result.stdout)


class UnusableVerifierTests(VerifierCase):
    """No verdict is not a good verdict."""

    def test_a_missing_repository_verifier_removes_nothing(self):
        fixture = self.installed(fan_active=True, enable=1)
        missing = os.path.join(fixture.harness, "not-here", "fanctl")

        result = fixture.uninstall(QNAP_TSX70_FAN_BINARY=missing)

        self.assertKeptEverything(result, fixture)
        self.assertIn("no fan-control program at %s" % missing, result.stderr)
        self.assertIn("could not be run", result.stderr)

    def test_an_unreadable_repository_verifier_removes_nothing(self):
        fixture = self.installed(fan_active=True, enable=1)
        unreadable = os.path.join(fixture.harness, "unreadable-verifier")
        with open(unreadable, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env python3\n")
        os.chmod(unreadable, 0o000)
        if os.access(unreadable, os.R_OK):
            # Running as root makes mode 000 readable, so the case has to be
            # reached the other way: a directory is a path python3 cannot run.
            unreadable = os.path.join(fixture.harness, "unreadable-dir")
            os.makedirs(unreadable, exist_ok=True)

        result = fixture.uninstall(QNAP_TSX70_FAN_BINARY=unreadable)

        self.assertKeptEverything(result, fixture)
        self.assertIn("could not be run", result.stderr)

    def test_a_verifier_the_interpreter_cannot_run_removes_nothing(self):
        # The file is there and readable and it is not a Python program. The
        # check has to be "does the chosen interpreter run it", not "does the
        # path exist".
        fixture = self.installed(fan_active=True, enable=1)
        broken = os.path.join(fixture.harness, "broken-verifier")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("this is not python(\n")
        os.chmod(broken, 0o755)

        result = fixture.uninstall(QNAP_TSX70_FAN_BINARY=broken)

        self.assertKeptEverything(result, fixture)
        self.assertIn("python3 cannot run %s" % broken, result.stderr)

    def test_the_chip_is_not_touched_when_the_verifier_cannot_run(self):
        # The refusal happens before anything is mutated, so the registers are
        # exactly as they were found.
        fixture = self.installed(fan_active=True, enable=1)
        before = (fixture.fan_register("pwm3_enable"),
                  fixture.fan_register("pwm3"))

        fixture.uninstall(QNAP_TSX70_FAN_BINARY=os.path.join(fixture.harness,
                                                             "absent"))

        self.assertEqual((fixture.fan_register("pwm3_enable"),
                          fixture.fan_register("pwm3")), before)

    def test_a_usable_verifier_is_the_positive_control(self):
        fixture = self.installed(fan_active=True, enable=1)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("verified with this repository's", result.stdout)
        self.assertFalse(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))


class RepoRootResolutionTests(unittest.TestCase):
    """The verifier is found from the script's own location.

    An operator's `sudo /opt/checkouts/qnap/scripts/uninstall.sh`, or a symlink
    to it on PATH, has to gate on that checkout's bin/qnap-tsx70-fancontrol -
    not on the working directory, and not on whatever is installed.
    """

    def setUp(self):
        self.fixture = ShellFixture(self)
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit("qnap-tsx70-lcd.service", active=True, enabled=True)
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=False,
                         enabled=True)
        fixture.with_fan_chip(enable=1)
        self.expected = os.path.join(REPO_ROOT, "bin", "qnap-tsx70-fancontrol")

    def run_uninstall(self, script):
        """A dry run, so nothing is removed and no register is written."""
        env = self.fixture.env()
        # The seam that normally aims the gate at the fixture's copy is the
        # thing under test here, so it has to go.
        env.pop("QNAP_TSX70_FAN_BINARY", None)
        return subprocess.run(["bash", script, "--dry-run"], cwd="/",
                              env=env, capture_output=True, text=True,
                              timeout=180)

    def test_the_default_verifier_is_the_repositorys_own(self):
        result = self.run_uninstall(os.path.join(REPO_ROOT,
                                                 "scripts/uninstall.sh"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry-run] python3 %s safe-state" % self.expected,
                      result.stdout)

    def test_a_symlinked_entry_point_resolves_the_same_repository(self):
        link = os.path.join(self.fixture.harness, "uninstall-link.sh")
        os.symlink(os.path.join(REPO_ROOT, "scripts/uninstall.sh"), link)

        result = self.run_uninstall(link)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry-run] python3 %s safe-state" % self.expected,
                      result.stdout,
                      "a symlinked uninstaller resolved a different tree")


if __name__ == "__main__":
    unittest.main()
