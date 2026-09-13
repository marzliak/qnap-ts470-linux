"""The uninstaller must not remove anything a fan writer is still using.

Removing the unit and the binary is what makes a surviving fan-control
process unrecoverable: systemd no longer knows about it, and the executable
that would have restored the chip's automatic mode is gone.
"""

import os
import unittest

from fixtures import ShellFixture


class UninstallCase(unittest.TestCase):
    def setUp(self):
        self.fixture = ShellFixture(self)

    def _installed(self, fan_active=False, fan_procs=()):
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit("qnap-tsx70-lcd.service", active=True, enabled=True)
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=fan_active,
                         enabled=True, procs=list(fan_procs))
        return fixture

    def assertArtifactsIntact(self, result):
        self.assertNotEqual(result.returncode, 0,
                            "the uninstaller should have refused:\n%s"
                            % result.stdout)
        for path in ("usr/local/bin/qnap-tsx70-fancontrol",
                     "usr/local/bin/qnap-tsx70-lcd",
                     "etc/systemd/system/qnap-tsx70-fancontrol.service"):
            self.assertTrue(self.fixture.exists(path),
                            "%s was removed despite the refusal" % path)


class FanStopTests(UninstallCase):
    """Group 3: a failed stop aborts before anything is removed."""

    def test_a_failed_fan_stop_aborts_before_removal(self):
        self._installed(fan_active=True)
        self.fixture.fail_verb("stop", "qnap-tsx70-fancontrol.service")

        result = self.fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertIn("could not be confirmed stopped", result.stderr)
        self.assertIn("safe-state", result.stderr)

    def test_a_stop_that_leaves_the_unit_active_aborts(self):
        self._installed(fan_active=True)
        self.fixture.make_stop_ineffective("qnap-tsx70-fancontrol.service")

        result = self.fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertIn("still active", result.stderr)

    def test_an_activating_unit_is_not_treated_as_stopped(self):
        # `systemctl is-active` exits nonzero for "activating"; a plain exit
        # status check would call a unit on its way up "already inactive".
        self._installed(fan_active=False)
        self.fixture.set_active_state("qnap-tsx70-fancontrol.service",
                                      "activating")
        self.fixture.make_stop_ineffective("qnap-tsx70-fancontrol.service")

        result = self.fixture.uninstall()

        self.assertArtifactsIntact(result)


class FanQuiescenceTests(UninstallCase):
    """Group 4: a surviving process aborts even when systemd says inactive."""

    def test_a_surviving_fan_process_aborts_the_uninstall(self):
        self._installed(fan_active=True, fan_procs=[8100])
        self.fixture.add_process(8100, "python3",
                                 exe=self.fixture.python_stub,
                                 argv=["python3",
                                       os.path.join(self.fixture.bin_dir,
                                                    "qnap-tsx70-fancontrol"),
                                       "run"],
                                 linger=True)

        result = self.fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertIn("8100", result.stderr)

    def test_a_versioned_interpreter_is_still_recognised(self):
        # /proc/<pid>/exe of a `#!/usr/bin/env python3` service resolves to
        # python3.13, not python3. Matching interpreters by exact name made
        # the whole quiescence gate silently pass.
        self._installed(fan_active=True, fan_procs=[8200])
        versioned = os.path.join(self.fixture.harness, "usr/bin/python3.13")
        with open(versioned, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(versioned, 0o755)
        self.fixture.add_process(8200, "python3", exe=versioned,
                                 argv=["python3",
                                       os.path.join(self.fixture.bin_dir,
                                                    "qnap-tsx70-fancontrol"),
                                       "run"],
                                 linger=True)

        result = self.fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertIn("8200", result.stderr)

    def test_a_clean_stop_removes_everything(self):
        self._installed(fan_active=True, fan_procs=[8300])
        self.fixture.add_process(8300, "python3",
                                 exe=self.fixture.python_stub,
                                 argv=["python3",
                                       os.path.join(self.fixture.bin_dir,
                                                    "qnap-tsx70-fancontrol")])

        result = self.fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))
        self.assertFalse(self.fixture.process_exists(8300))


class SafeStateClaimTests(UninstallCase):
    """Automatic mode is only claimed when it was actually confirmed."""

    def test_safe_state_runs_only_after_the_writer_is_gone(self):
        fixture = self._installed(fan_active=True)
        marker = os.path.join(fixture.harness, "safe-state-ran")
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\n[ \"$1\" = safe-state ] && : > %s\nexit 0\n"
                      % marker, 0o755)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(marker), "safe-state was never invoked")
        self.assertIn("automatic mode (verified)", result.stdout)

    def test_a_failing_safe_state_is_not_reported_as_automatic_mode(self):
        fixture = self._installed(fan_active=True)
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\nexit 1\n", 0o755)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("automatic mode (verified)", result.stdout)
        self.assertIn("NOT verified", result.stderr)

    def test_nothing_is_claimed_when_fan_control_was_never_installed(self):
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit("qnap-tsx70-lcd.service", active=True, enabled=True)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fan control was not installed", result.stdout)


class UninstallDryRunTests(UninstallCase):
    def test_a_dry_run_changes_nothing(self):
        self._installed(fan_active=True, fan_procs=[8400])
        self.fixture.add_process(8400, "python3",
                                 exe=self.fixture.python_stub,
                                 argv=["python3",
                                       os.path.join(self.fixture.bin_dir,
                                                    "qnap-tsx70-fancontrol")])
        before = self.fixture.snapshot()

        result = self.fixture.uninstall("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.fixture.mutating_calls(), [])
        self.assertTrue(self.fixture.process_exists(8400))


if __name__ == "__main__":
    unittest.main()


class ReadOnlyProcessTests(UninstallCase):
    """A `status` in another terminal is not a reason to refuse."""

    def test_a_read_only_subcommand_does_not_block_the_uninstall(self):
        self._installed(fan_active=False)
        self.fixture.add_process(8500, "python3",
                                 exe=self.fixture.python_stub,
                                 argv=["python3",
                                       os.path.join(self.fixture.bin_dir,
                                                    "qnap-tsx70-fancontrol"),
                                       "status"],
                                 linger=True)

        result = self.fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))


class LoadedButFilelessUnitTests(UninstallCase):
    """A unit whose file is already gone can still be running."""

    def test_a_loaded_unit_with_no_file_is_still_stopped(self):
        self._installed(fan_active=True)
        self.fixture.forget_unit_file("qnap-tsx70-fancontrol.service")

        result = self.fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("stop qnap-tsx70-fancontrol.service",
                      self.fixture.calls(),
                      "a loaded unit with no file was never stopped")
