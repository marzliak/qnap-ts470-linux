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
        # The order is the point: the quiescence gate first, then the
        # handback. Asking the chip while a writer is alive is answered by
        # whichever of the two wrote last. The invocation is observed on the
        # repository verifier, which is the program the gate actually runs.
        fixture = self._installed(fan_active=True)
        fixture.with_fan_chip(enable=1)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("safe-state", " ".join(fixture.fan_commands()),
                      "the repository verifier was never invoked")
        self.assertLess(result.stdout.index("no fan-control process remains"),
                        result.stdout.index("Handing the fans back"),
                        "the handback ran before the quiescence gate")
        self.assertIn("automatic mode (verified)", result.stdout)

    def test_a_failing_safe_state_stops_the_uninstall(self):
        # This used to warn and carry on, deleting the binary and the unit
        # anyway - so the one command that could have put the fans back was
        # removed precisely because it had reported that it could not.
        #
        # The failure is now injected where the verdict actually comes from:
        # the chip, as read back by this repository's verifier. An installed
        # binary exiting 1 decides nothing here, and neither does one exiting
        # 0 - see tests/test_uninstall_verifier.py.
        fixture = self._installed(fan_active=True)
        fixture.with_fan_chip(enable=1, ignore_writes=["pwm3_enable"])

        result = fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertNotIn("automatic mode (verified)", result.stdout)
        self.assertIn("NOT been", result.stderr)
        self.assertIn("nothing was removed", result.stderr)

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


class ArtifactGateTests(UninstallCase):
    """Whatever the removal step deletes, the fan gate has to have seen."""

    def test_a_non_executable_binary_is_still_gated(self):
        # The gate tested `[ -x ]` while the removal tested `[ -f ]`, so a
        # binary left mode 0644 skipped the whole quiescence check and was
        # deleted anyway - out from under a live writer.
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\nexit 0\n", 0o644)
        fixture.add_process(8400, "python3", exe=fixture.python_stub_versioned,
                            argv=["python3",
                                  os.path.join(fixture.bin_dir,
                                               "qnap-tsx70-fancontrol"),
                                  "run"],
                            linger=True)

        result = fixture.uninstall()

        self.assertNotEqual(result.returncode, 0,
                            "a live writer did not stop the uninstall")
        self.assertTrue(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"),
                        "the fan binary was deleted under a live writer")
        self.assertTrue(fixture.process_exists(8400))

    def test_a_writer_with_no_artifacts_left_still_blocks(self):
        # No unit, no binary, just a process. Nothing to remove, but the
        # summary must not tell the operator the chip is in charge.
        fixture = self.fixture
        fixture.add_process(8401, "python3", exe=fixture.python_stub_versioned,
                            argv=["python3",
                                  "/usr/local/bin/qnap-tsx70-fancontrol",
                                  "run"],
                            linger=True)

        result = fixture.uninstall()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("already in charge of the fans", result.stdout)

    def test_a_clean_tree_with_no_fan_control_says_so(self):
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit("qnap-tsx70-lcd.service", active=True, enabled=True)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fan control was not installed", result.stdout)

    def test_a_dry_run_claims_neither_removal_nor_automatic_mode(self):
        self._installed(fan_active=True)

        result = self.fixture.uninstall("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("automatic mode (verified)", result.stdout,
                         "a preview claimed a safe state it never asked for")
        self.assertNotIn("ok   removed", result.stdout,
                         "a preview reported removals it did not perform")
        self.assertIn("Nothing was changed", result.stdout)


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


class FanCommandClassificationTests(UninstallCase):
    """The command word decides, not whether a word appears somewhere in argv.

    Asking whether *any* argv token equalled `status`, `validate-cache`,
    `--version` or `--help` classified `qnap-tsx70-fancontrol run --sensor
    status` - a live control loop - as read-only. The unit and the binary were
    then removed under it, which is exactly the state this script exists to
    prevent: a process driving PWM registers that systemd no longer knows
    about and whose safe-state executable is gone.
    """

    def _writer(self, argv, pid=8600, linger=True):
        self._installed(fan_active=False)
        self.fixture.add_process(pid, "python3",
                                 exe=self.fixture.python_stub_versioned,
                                 argv=["python3",
                                       os.path.join(self.fixture.bin_dir,
                                                    "qnap-tsx70-fancontrol")]
                                      + list(argv),
                                 linger=linger)
        return self.fixture

    def assertRefused(self, result, pid=8600):
        self.assertArtifactsIntact(result)
        self.assertIn(str(pid), result.stderr)
        self.assertTrue(self.fixture.process_exists(pid))
        self.assertTrue(self.fixture.exists("etc/systemd/system/qnap-tsx70-lcd.service"),
                        "the LCD unit went while a fan writer was alive")

    def test_a_run_whose_sensor_is_called_status_still_blocks(self):
        self._writer(["run", "--sensor", "status"])

        result = self.fixture.uninstall()

        self.assertRefused(result)

    def test_the_equals_form_blocks_too(self):
        self._writer(["run", "--sensor=status"])

        result = self.fixture.uninstall()

        self.assertRefused(result)

    def test_a_cache_path_ending_in_a_read_only_word_is_no_defence(self):
        self._writer(["run", "--cache", "/var/tmp/validate-cache"])

        result = self.fixture.uninstall()

        self.assertRefused(result)

    def test_calibrate_blocks(self):
        self._writer(["calibrate", "--yes", "--sensor", "status"])

        result = self.fixture.uninstall()

        self.assertRefused(result)

    def test_safe_state_blocks(self):
        # It writes registers too, and it is the command the recovery hint
        # tells the operator to run - after this script has got out of the way.
        self._writer(["safe-state", "--cache", "status"])

        result = self.fixture.uninstall()

        self.assertRefused(result)

    def test_an_invocation_with_no_command_blocks(self):
        self._writer([])

        result = self.fixture.uninstall()

        self.assertRefused(result)

    def test_an_unknown_command_blocks(self):
        self._writer(["tune", "--sensor", "status"])

        result = self.fixture.uninstall()

        self.assertRefused(result)

    def test_a_status_with_its_own_options_is_still_read_only(self):
        self._writer(["status", "--json", "--cache", "/x"])

        result = self.fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertTrue(self.fixture.process_exists(8600),
                        "a read-only process was signalled")

    def test_validate_cache_is_still_read_only(self):
        self._writer(["validate-cache", "--cache", "/x/y.json"])

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


class SafeStateGateTests(UninstallCase):
    """An unverified safe state is a gate, not a line in a summary.

    These run the shipped `bin/qnap-tsx70-fancontrol` for real against a fake
    f71882fg device, so what the uninstaller trusts is what safe-state
    actually verifies rather than a stub that agrees with it.
    """

    def _with_chip(self, **chip):
        fixture = self._installed(fan_active=True)
        fixture.with_fan_chip(**chip)
        return fixture

    def assertNothingClaimed(self, result):
        self.assertNotIn("(verified)", result.stdout,
                         "automatic mode was claimed after it failed")
        self.assertNotIn("ok   removed", result.stdout,
                         "a removal was reported after the refusal")
        self.assertIn("nothing was removed", result.stderr)
        self.assertIn("safe-state", result.stderr,
                      "the refusal gave no way to recover")

    def test_a_verified_safe_state_lets_the_removal_proceed(self):
        # The positive control: the same real binary, on a chip that takes the
        # write. Without this the gate could be refusing for any reason.
        fixture = self._with_chip(enable=1)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertEqual(fixture.fan_register("pwm3_enable"), "2")
        self.assertEqual(fixture.fan_register("pwm3"), "255")
        self.assertIn("safe-state", " ".join(fixture.fan_commands()))
        self.assertFalse(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertIn("automatic mode (verified)", result.stdout)

    def test_a_retained_manual_readback_stops_the_uninstall(self):
        # The chip took the write to pwm3_enable and stayed in manual mode.
        fixture = self._with_chip(enable=1, ignore_writes=["pwm3_enable"])

        result = fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertNothingClaimed(result)
        self.assertIn("still in mode 1", result.stderr)
        self.assertEqual(fixture.fan_register("pwm3_enable"), "1")

    def test_an_unreadable_mode_stops_the_uninstall(self):
        # The register answers with nothing at all. "Unreadable" used to be
        # accepted as automatic mode, which is the readback that matters least
        # and the state that matters most.
        fixture = self._with_chip(enable="", ignore_writes=["pwm3_enable"])

        result = fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertNothingClaimed(result)
        self.assertIn("unreadable", result.stderr)

    def test_a_chip_with_no_controllable_channel_stops_the_uninstall(self):
        # Tachometers, no duty registers: nothing here can be handed back.
        fixture = self._with_chip(controllable=False)

        result = fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertNothingClaimed(result)
        self.assertIn("no controllable channel", result.stderr)

    def test_a_failed_register_write_stops_the_uninstall(self):
        fixture = self._with_chip(enable=1, fail_writes=["pwm3_enable"])

        result = fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertNothingClaimed(result)
        self.assertIn("did not accept the safe-state write", result.stderr)

    def test_one_bad_channel_out_of_three_stops_the_uninstall(self):
        fixture = self._with_chip(channels=(1, 2, 3), enable=1,
                                  ignore_writes=["pwm2_enable"])

        result = fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertNothingClaimed(result)
        # Best effort first: the other two were still handed back.
        for channel in (1, 3):
            self.assertEqual(fixture.fan_register("pwm%d_enable" % channel), "2")

    def test_the_installed_binarys_mode_is_not_what_decides(self):
        # The installed binary is not executable, so under the old gate the
        # run refused. It is not run any more - the repository's verifier is -
        # so its mode says nothing about whether the fans are back, and the
        # verdict comes from the chip either way. The removal proceeds because
        # a real handback was proven, not because a file was chmod'd.
        fixture = self._with_chip(enable=1)
        os.chmod(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"), 0o644)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertEqual(fixture.fan_register("pwm3_enable"), "2")
        self.assertIn("safe-state", " ".join(fixture.fan_commands()))
        self.assertFalse(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))

    def test_a_dry_run_previews_the_repository_verifier(self):
        # The preview has to name the program the real run would use, because
        # that is the command an operator checks before trusting the gate.
        fixture = self._with_chip(enable=1)
        before = fixture.snapshot()

        result = fixture.uninstall("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertIn("[dry-run] python3 %s safe-state" % fixture.fan_binary,
                      result.stdout)
        self.assertIn("gated on its exit status", result.stdout)
        self.assertNotIn("(verified)", result.stdout)
        self.assertEqual(fixture.fan_commands(), [],
                         "a dry run invoked the verifier for real")

    def test_the_lcd_is_left_alone_when_the_gate_trips(self):
        # Transactional: the run stops before the LCD service is touched
        # rather than half-uninstalling the machine.
        fixture = self._with_chip(enable=1, ignore_writes=["pwm3_enable"])

        result = fixture.uninstall()

        self.assertArtifactsIntact(result)
        self.assertTrue(fixture.exists("etc/systemd/system/qnap-tsx70-lcd.service"))
        self.assertNotIn("stop qnap-tsx70-lcd.service", fixture.calls(),
                         "the LCD was stopped by a run that then refused")

    def test_a_leftover_unit_with_no_binary_is_still_gated(self):
        # There is no installed binary to execute, and there used to be no
        # gate either: the unit was deleted with a warning that automatic mode
        # had never been confirmed. A unit file is a recovery mechanism of its
        # own - `systemctl start` runs ExecStopPost= on the way back down - so
        # deleting it unproven removes one. The repository's verifier is
        # always available, so the gate runs and the removal is earned.
        fixture = self.fixture
        fixture.with_fan_controller(enable=1)
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=False,
                         enabled=True)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            fixture.exists("etc/systemd/system/qnap-tsx70-fancontrol.service"))
        self.assertEqual(fixture.fan_register("pwm3_enable"), "2")
        self.assertIn("automatic mode (verified)", result.stdout)

    def test_a_leftover_unit_is_kept_when_the_handback_cannot_be_proven(self):
        # The same machine, on a chip that will not leave manual mode. The
        # unit stays: it is the only way left to ask the chip again.
        fixture = self.fixture
        fixture.with_fan_controller(enable=1, ignore_writes=["pwm3_enable"])
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=False,
                         enabled=True)

        result = fixture.uninstall()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(
            fixture.exists("etc/systemd/system/qnap-tsx70-fancontrol.service"))
        self.assertIn("nothing was removed", result.stderr)


class FanOnlyTests(UninstallCase):
    """--fan-only is the documented way to remove just fan control."""

    def test_only_the_fan_artifacts_go(self):
        fixture = self._with_lcd_and_fan()

        result = fixture.uninstall("--fan-only")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertFalse(
            fixture.exists("etc/systemd/system/qnap-tsx70-fancontrol.service"))
        self.assertTrue(fixture.exists("usr/local/bin/qnap-tsx70-lcd"),
                        "--fan-only removed the LCD binary")
        self.assertTrue(
            fixture.exists("etc/systemd/system/qnap-tsx70-lcd.service"),
            "--fan-only removed the LCD unit")
        self.assertTrue(fixture.exists("etc/qnap-tsx70-lcd.conf"))
        self.assertNotIn("stop qnap-tsx70-lcd.service", fixture.calls(),
                         "--fan-only stopped the LCD service")

    def test_an_unverified_safe_state_removes_nothing(self):
        fixture = self._with_lcd_and_fan(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = fixture.uninstall("--fan-only")

        self.assertArtifactsIntact(result)
        self.assertIn("nothing was removed", result.stderr)

    def test_purge_with_fan_only_keeps_the_lcd_configuration(self):
        fixture = self._with_lcd_and_fan()
        fixture.write(os.path.join(fixture.state_dir, "fan-calibration.json"),
                      "{}\n")

        result = fixture.uninstall("--fan-only", "--purge")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fixture.exists("etc/qnap-tsx70-lcd.conf"),
                        "--fan-only --purge deleted the LCD configuration")
        self.assertFalse(fixture.exists("var/lib/qnap-tsx70"))

    def _with_lcd_and_fan(self, **chip):
        fixture = self._installed(fan_active=True)
        fixture.with_config()
        fixture.with_fan_chip(**chip)
        return fixture
