"""Behavioural regression tests for the installer's safety gates.

Every test here runs scripts/install.sh for real against a fake root with fake
systemctl, fuser and /proc. Nothing greps the source: what is asserted is what
the script did - which files exist afterwards, with which content and mode,
which systemctl calls were made, and whether anything was signalled.
"""

import json
import os
import unittest

from fixtures import VALID_CACHE, ShellFixture
from helpers import REPO_ROOT

INVALID_CACHE = '{"channels": [1], '  # truncated on purpose

# The backup directory and the path leading to it are supposed to outlive a
# rollback - it is where the manifest and the originals are kept.
BACKUP_PATHS = ("var", "var/backups")


def without_backups(snapshot):
    return {path: value for path, value in snapshot.items()
            if path not in BACKUP_PATHS and not path.startswith("var/backups/")}


def repo_file(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as fh:
        return fh.read()


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.fixture = ShellFixture(self)

    def assertNothingRemoved(self, result):
        """The legacy installation must survive an aborted install intact."""
        self.assertNotEqual(result.returncode, 0,
                            "the installer should have aborted:\n%s"
                            % result.stdout)
        for path in ("usr/local/bin/saturn-lcd",
                     "etc/systemd/system/saturn-lcd.service"):
            self.assertTrue(self.fixture.exists(path),
                            "%s was removed by an aborted install" % path)


class SerialOwnershipTests(InstallerCase):
    """Group 1: the port's owner decides whether migration may proceed."""

    def test_migration_with_the_legacy_lcd_holding_the_port_succeeds(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=True)
        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("stop saturn-lcd.service", self.fixture.calls())
        self.assertIn("restart qnap-tsx70-lcd.service", self.fixture.calls())
        self.assertEqual(self.fixture.read("usr/local/bin/qnap-tsx70-lcd"),
                         repo_file("bin/qnap-tsx70-lcd"))
        self.assertFalse(self.fixture.exists("usr/local/bin/saturn-lcd"))
        self.assertFalse(self.fixture.exists("etc/systemd/system/saturn-lcd.service"))

    def test_the_planned_stop_happens_before_the_new_service_starts(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=True)
        self.assertEqual(self.fixture.install("--skip-deps").returncode, 0)
        calls = self.fixture.calls()
        self.assertLess(calls.index("stop saturn-lcd.service"),
                        calls.index("restart qnap-tsx70-lcd.service"))

    def test_an_unknown_process_holding_the_port_blocks_the_install(self):
        self.fixture.with_config()
        self.fixture.add_process(9999, "socat", exe="/usr/bin/socat",
                                 argv=["socat", "-", "/dev/ttyS1"])
        self.fixture.hold_port(9999)
        before = self.fixture.snapshot()

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not recognise", result.stderr)
        self.assertEqual(self.fixture.snapshot(), before,
                         "a refused preflight changed the system")
        self.assertEqual(self.fixture.mutating_calls(), [])

    def test_force_does_not_override_an_unknown_serial_owner(self):
        # --force exists for absent hardware. Letting it also mean "ignore the
        # process on the port" is exactly the bypass this must not become.
        self.fixture.with_config()
        self.fixture.add_process(9999, "socat", exe="/usr/bin/socat")
        self.fixture.hold_port(9999)

        result = self.fixture.install("--skip-deps", "--force")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--force deliberately does not cover this case",
                      result.stderr)

    def test_allow_serial_owner_passes_preflight_but_still_signals_nothing(self):
        self.fixture.with_config()
        self.fixture.add_process(9999, "socat", exe="/usr/bin/socat")
        self.fixture.hold_port(9999)

        result = self.fixture.install("--skip-deps", "--allow-serial-owner", "9999")

        self.assertIn("preflight passed", result.stdout)
        self.assertEqual(self.fixture.kill_calls(), [],
                         "an unrecognised process was signalled")
        self.assertTrue(self.fixture.process_exists(9999))

    def test_allow_serial_owner_still_refuses_while_the_port_stays_busy(self):
        self.fixture.with_config()
        self.fixture.add_process(9999, "socat", exe="/usr/bin/socat")
        self.fixture.hold_port(9999)

        result = self.fixture.install("--skip-deps", "--allow-serial-owner", "9999")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is still held by", result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))

    def test_a_non_numeric_allow_serial_owner_is_rejected(self):
        self.fixture.with_config()
        result = self.fixture.install("--skip-deps", "--allow-serial-owner", "init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("numeric PID", result.stderr)


class ReinstallTests(InstallerCase):
    """Group 2: reinstalling over a running install is a normal operation."""

    def test_reinstall_with_the_current_service_active_succeeds(self):
        self.fixture.with_config()
        self.fixture.with_current_install(active=True)

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        calls = self.fixture.calls()
        self.assertIn("stop qnap-tsx70-lcd.service", calls)
        self.assertLess(calls.index("stop qnap-tsx70-lcd.service"),
                        calls.index("restart qnap-tsx70-lcd.service"))
        self.assertEqual(self.fixture.read("usr/local/bin/qnap-tsx70-lcd"),
                         repo_file("bin/qnap-tsx70-lcd"),
                         "the stale binary was not replaced")


class FanLifecycleTests(InstallerCase):
    """Groups 3 and 4: no artifact goes away while a fan writer might live."""

    def test_a_failed_fan_stop_aborts_before_anything_is_removed(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        self.fixture.add_unit("saturn-fancontrol.service", active=True,
                              enabled=True)
        self.fixture.fail_verb("stop", "saturn-fancontrol.service")

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        self.assertIn("systemctl stop saturn-fancontrol.service failed",
                      result.stderr)
        self.assertIn("safe-state", result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))

    def test_a_stop_that_leaves_the_unit_active_aborts(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        self.fixture.add_unit("saturn-fancontrol.service", active=True)
        self.fixture.make_stop_ineffective("saturn-fancontrol.service")

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        self.assertIn("still active", result.stderr)

    def test_a_fan_process_that_survives_the_stop_aborts_the_install(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        self.fixture.add_unit("saturn-fancontrol.service", active=True,
                              procs=[7001])
        # linger: systemd reports the unit stopped, the writer is still there.
        self.fixture.add_process(7001, "saturn-fancontrol",
                                 exe="/usr/local/bin/saturn-fancontrol",
                                 linger=True)

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        self.assertIn("7001", result.stderr)
        self.assertIn("still running", result.stderr)


class LegacyPidTests(InstallerCase):
    """Group 5: the PID file is evidence, not an instruction."""

    def _legacy_with_pid(self, content="4242\n"):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        self.fixture.write(self.fixture.legacy_pid, content)

    def test_a_validated_legacy_process_is_stopped_and_then_cleaned_up(self):
        self._legacy_with_pid()
        self.fixture.add_process(4242, "saturn-fancontrol",
                                 exe="/usr/local/bin/saturn-fancontrol")

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("-TERM 4242", " ".join(self.fixture.kill_calls()))
        self.assertFalse(self.fixture.process_exists(4242))
        self.assertFalse(self.fixture.exists("run/saturn-fancontrol.pid"))

    def test_a_stale_pid_file_is_cleaned_up_without_signalling_anything(self):
        self._legacy_with_pid()  # no process with that pid exists

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.kill_calls(), [])
        self.assertIn("is stale", result.stdout)
        self.assertFalse(self.fixture.exists("run/saturn-fancontrol.pid"))

    def test_a_malformed_pid_file_aborts_and_signals_nothing(self):
        self._legacy_with_pid(content="not-a-pid\n")

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        self.assertEqual(self.fixture.kill_calls(), [])
        self.assertIn("does not contain a PID", result.stderr)
        self.assertTrue(self.fixture.exists("run/saturn-fancontrol.pid"),
                        "a PID file nobody understood was deleted anyway")

    def test_an_empty_pid_file_naming_nobody_is_not_a_blocker(self):
        # It names no process, so with no legacy fan running there is nothing
        # to be careful about. Blocking here would only teach people --force.
        self._legacy_with_pid(content="\n")
        result = self.fixture.install("--skip-deps")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.kill_calls(), [])

    def test_an_empty_pid_file_aborts_while_a_legacy_fan_is_running(self):
        self._legacy_with_pid(content="\n")
        self.fixture.add_process(6100, "saturn-fancontrol",
                                 exe="/usr/local/bin/saturn-fancontrol")
        result = self.fixture.install("--skip-deps")
        self.assertNothingRemoved(result)
        self.assertEqual(self.fixture.kill_calls(), [])

    def test_a_reused_pid_belonging_to_another_process_is_never_signalled(self):
        self._legacy_with_pid()
        self.fixture.add_process(4242, "sshd", exe="/usr/sbin/sshd",
                                 argv=["sshd", "-D"])

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        self.assertEqual(self.fixture.kill_calls(), [],
                         "an unrelated process was signalled")
        self.assertIn("PID was almost certainly reused", result.stderr)
        self.assertTrue(self.fixture.process_exists(4242))

    def test_a_command_line_mention_is_not_an_identity(self):
        # `cat /usr/local/bin/saturn-fancontrol` names the binary without
        # being it. Matching on argv alone would have signalled this.
        self._legacy_with_pid()
        self.fixture.add_process(4242, "cat", exe="/usr/bin/cat",
                                 argv=["cat", "/usr/local/bin/saturn-fancontrol"])

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        self.assertEqual(self.fixture.kill_calls(), [])

    def test_a_legacy_daemon_under_a_versioned_interpreter_is_recognised(self):
        # /proc/<pid>/exe of a `#!/usr/bin/env python3` script resolves to
        # python3.13. Matching interpreter names exactly made the installer
        # call its own legacy daemon a reused PID and refuse to migrate.
        # A unit written as `ExecStart=/usr/bin/python3.13 /usr/local/bin/...`
        # reports the version in comm as well as in exe, so neither name
        # matches an exact-match interpreter list.
        self._legacy_with_pid()
        self.fixture.add_process(
            4242, "python3.13", exe=self.fixture.python_stub_versioned,
            argv=["/usr/bin/python3.13", "/usr/local/bin/saturn-fancontrol",
                  "--run"])

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("-TERM 4242", " ".join(self.fixture.kill_calls()))
        self.assertNotIn("reused", result.stderr)

    def test_a_process_that_ignores_sigterm_aborts_instead_of_being_killed(self):
        self._legacy_with_pid()
        self.fixture.add_process(4242, "saturn-fancontrol",
                                 exe="/usr/local/bin/saturn-fancontrol",
                                 linger=True)

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        signals = " ".join(self.fixture.kill_calls())
        self.assertIn("-TERM", signals)
        self.assertNotIn("-KILL", signals,
                         "SIGKILL would skip the handler that frees the fans")
        self.assertTrue(self.fixture.exists("run/saturn-fancontrol.pid"))


class DryRunTests(InstallerCase):
    """Group 6: --dry-run must not touch anything, rollback included."""

    def test_a_dry_run_migration_changes_nothing(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=True)
        self.fixture.write(self.fixture.legacy_pid, "4242\n")
        self.fixture.add_process(4242, "saturn-fancontrol",
                                 exe="/usr/local/bin/saturn-fancontrol")
        before = self.fixture.snapshot()

        result = self.fixture.install("--skip-deps", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.snapshot(), before,
                         "the dry run modified the fake root")
        self.assertEqual(self.fixture.mutating_calls(), [])
        self.assertEqual(self.fixture.kill_calls(), [])
        self.assertTrue(self.fixture.process_exists(4242))

    def test_a_dry_run_rollback_after_an_injected_failure_changes_nothing(self):
        # rollback() used to call systemctl, rm and cp directly, so a trapped
        # failure during a dry run could mutate the host for real.
        probe = self._probe_with_failure_in_install_files()
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=True)
        before = self.fixture.snapshot()

        result = self.fixture.run_script(probe, "--skip-deps", "--dry-run")

        self.assertNotEqual(result.returncode, 0,
                            "the injected failure did not fail the run")
        self.assertIn("rolling back", result.stderr)
        # The report-only path has to be the one that ran. Without this the
        # test also passes when rollback falls through to the real restore
        # and is stopped only by the manifest that a dry run never writes.
        self.assertIn("[dry-run] rollback would restore", result.stdout)
        self.assertIn("[dry-run]   path", result.stdout)
        self.assertEqual(self.fixture.snapshot(), before,
                         "a dry-run rollback modified the fake root")
        self.assertEqual(self.fixture.mutating_calls(), [])
        self.assertEqual(self.fixture.kill_calls(), [])

    def _probe_with_failure_in_install_files(self):
        """A copy of the installer that fails at the first install step.

        It lives beside the original so its REPO_ROOT still resolves.
        """
        relative = "scripts/.install-dry-run-probe.sh"
        path = os.path.join(REPO_ROOT, relative)
        source = repo_file("scripts/install.sh")
        marker = ('    step "Installing files"\n'
                  '    txn_mkdir "$STATE_DIR" 0755')
        self.assertIn(marker, source, "the probe's injection point moved")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source.replace(marker, "    false\n" + marker, 1))
        os.chmod(path, 0o755)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return relative


class RollbackPhaseTests(InstallerCase):
    """Group 7: a failure in any phase puts the previous state back."""

    def _rich_prior_state(self, with_cache=True):
        fixture = self.fixture
        fixture.with_config()
        fixture.with_legacy(lcd_active=True,
                            cache=VALID_CACHE if with_cache else None)
        # Pre-existing new-name artifacts: rollback must restore these, not
        # delete them for having the new name.
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\necho previous\n", 0o700)
        fixture.write(os.path.join(fixture.unit_dir, "qnap-tsx70-lcd.service"),
                      "[Unit]\nDescription=previous\n", 0o600)
        fixture.add_unit("qnap-tsx70-lcd.service", active=False, enabled=False,
                         content="[Unit]\nDescription=previous\n")
        return fixture.snapshot()

    def _assert_restored(self, before, result):
        self.assertNotEqual(result.returncode, 0,
                            "the phase failure did not fail the install")
        self.assertEqual(without_backups(self.fixture.snapshot()),
                         without_backups(before),
                         "rollback did not restore the tree")
        self.assertTrue(self.fixture.unit_state("saturn-lcd.service")["active"],
                        "the legacy service was not started again")
        self.assertTrue(self.fixture.unit_state("saturn-lcd.service")["enabled"])
        self.assertFalse(self.fixture.unit_state("qnap-tsx70-lcd.service")["enabled"],
                         "a unit this run enabled stayed enabled")

    def test_a_failure_installing_files_rolls_back(self):
        before = self._rich_prior_state(with_cache=False)
        # A regular file where the state directory belongs: `install -d` and
        # `mkdir -p` both fail on it.
        self.fixture.write(self.fixture.state_dir, "not a directory\n")
        before = self.fixture.snapshot()
        result = self.fixture.install("--skip-deps")
        self._assert_restored(before, result)

    def test_a_failure_enabling_the_service_rolls_back(self):
        before = self._rich_prior_state()
        self.fixture.fail_verb("enable", "qnap-tsx70-lcd.service")
        self._assert_restored(before, self.fixture.install("--skip-deps"))

    def test_a_failure_restarting_the_service_rolls_back(self):
        before = self._rich_prior_state()
        self.fixture.fail_verb("restart", "qnap-tsx70-lcd.service")
        self._assert_restored(before, self.fixture.install("--skip-deps"))

    def test_a_service_that_never_becomes_active_rolls_back(self):
        before = self._rich_prior_state()
        self.fixture.make_start_inert("qnap-tsx70-lcd.service")
        self._assert_restored(before, self.fixture.install("--skip-deps"))

    def test_a_failure_during_legacy_cleanup_rolls_back(self):
        # The trap used to be disarmed before this point, so a cleanup failure
        # left a half-migrated system behind with no rollback at all. There are
        # two normal daemon-reloads: the second belongs to remove_legacy().
        self._rich_prior_state()
        self.fixture.fail_verb("daemon-reload", "", after=1)
        before = self.fixture.snapshot()

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(without_backups(self.fixture.snapshot()),
                         without_backups(before),
                         "a cleanup failure was not rolled back")

    def test_the_backup_and_manifest_survive_a_rollback(self):
        self._rich_prior_state()
        self.fixture.fail_verb("restart", "qnap-tsx70-lcd.service")
        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0)
        backups = self.fixture.backups()
        self.assertEqual(len(backups), 1, backups)
        self.assertIn(backups[0], result.stderr)
        for relative in ("txn/manifest.tsv", "txn/units.tsv", "bin/saturn-lcd"):
            self.assertTrue(os.path.exists(os.path.join(backups[0], relative)),
                            "missing %s in the backup" % relative)

    def test_a_rollback_whose_own_steps_fail_does_not_recurse(self):
        # `set +e` does not remove an inherited ERR trap: a failing restore
        # used to re-enter the handler.
        self._rich_prior_state()
        self.fixture.fail_verb("restart", "qnap-tsx70-lcd.service")
        self.fixture.fail_verb("start", "saturn-lcd.service")
        self.fixture.fail_verb("daemon-reload", "", after=1)

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.count("rolling back to the previous"), 1,
                         "rollback ran more than once:\n%s" % result.stderr)
        self.assertIn("problem(s)", result.stderr)


class StateMatrixTests(InstallerCase):
    """Group 8: whatever was there before is what comes back."""

    ARTIFACTS = ("new_bin", "new_unit", "config", "legacy_bin", "legacy_unit",
                 "legacy_cache", "modules")

    SCENARIOS = (
        ("all present", dict.fromkeys(ARTIFACTS, True)),
        ("all absent", dict.fromkeys(ARTIFACTS, False)),
        ("mixed a", {"new_bin": True, "new_unit": False, "config": True,
                     "legacy_bin": False, "legacy_unit": True,
                     "legacy_cache": False, "modules": True}),
        ("mixed b", {"new_bin": False, "new_unit": True, "config": False,
                     "legacy_bin": True, "legacy_unit": False,
                     "legacy_cache": True, "modules": False}),
    )

    def _arrange(self, present):
        fixture = self.fixture
        # The configuration always has to name the port; when the scenario
        # says "no config", it is the default path that must stay absent.
        if present["config"]:
            fixture.with_config(extra="page_interval = 9\n")
        else:
            os.environ.setdefault("QNAP_TSX70_UNUSED", "")
            fixture.write(os.path.join(fixture.root, "etc/keep-port"), "")
        if present["new_bin"]:
            fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                          "#!/bin/sh\necho previous\n", 0o700)
        if present["new_unit"]:
            fixture.write(os.path.join(fixture.unit_dir,
                                       "qnap-tsx70-lcd.service"),
                          "[Unit]\nDescription=previous\n", 0o600)
            fixture.add_unit("qnap-tsx70-lcd.service", active=False,
                             enabled=True,
                             content="[Unit]\nDescription=previous\n")
        if present["legacy_bin"]:
            fixture.write(os.path.join(fixture.bin_dir, "saturn-lcd"),
                          "#!/bin/sh\necho legacy\n", 0o755)
        if present["legacy_unit"]:
            fixture.add_unit("saturn-lcd.service", active=True, enabled=True)
        if present["legacy_cache"]:
            fixture.write(fixture.legacy_cache, VALID_CACHE, 0o640)
        if present["modules"]:
            fixture.write(fixture.modules_file, "coretemp\n", 0o644)

    def test_rollback_restores_every_starting_state_exactly(self):
        for name, present in self.SCENARIOS:
            with self.subTest(scenario=name):
                self.fixture = ShellFixture(self)
                self._arrange(present)
                # The port must be configured for preflight to find it.
                if not present["config"]:
                    self.fixture.with_config()
                    self.fixture.write(self.fixture.conf_file,
                                       "serial_port = %s\n" % self.fixture.port)
                before = self.fixture.snapshot()
                self.fixture.fail_verb("restart", "qnap-tsx70-lcd.service")

                result = self.fixture.install("--skip-deps", "--with-fan-control",
                                              "--force")

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(without_backups(self.fixture.snapshot()),
                                 without_backups(before),
                                 "%s: rollback did not restore the tree" % name)
                if present["legacy_unit"]:
                    state = self.fixture.unit_state("saturn-lcd.service")
                    self.assertTrue(state["active"] and state["enabled"])


class CacheMigrationTests(InstallerCase):
    """Group 9: an invalid cache is preserved, never quietly deleted."""

    def test_a_valid_cache_migrates_and_the_legacy_copy_goes(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False, cache=VALID_CACHE)

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        migrated = self.fixture.read("var/lib/qnap-tsx70/fan-calibration.json")
        self.assertEqual(json.loads(migrated), json.loads(VALID_CACHE))
        self.assertFalse(self.fixture.exists("etc/saturn-fan-cache.json"))
        backup = self.fixture.backups()[0]
        self.assertTrue(os.path.exists(
            os.path.join(backup, "state/saturn-fan-cache.json")))

    def test_an_invalid_cache_stays_where_it_is_and_is_reported(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False, cache=INVALID_CACHE)

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertEqual(self.fixture.read("etc/saturn-fan-cache.json"),
                         INVALID_CACHE, "the invalid cache was altered")
        self.assertFalse(
            self.fixture.exists("var/lib/qnap-tsx70/fan-calibration.json"),
            "an invalid cache was migrated anyway")
        backup = self.fixture.backups()[0]
        self.assertTrue(os.path.exists(
            os.path.join(backup, "state/saturn-fan-cache.json")))
        self.assertIn("did not validate", result.stderr)
        self.assertIn(self.fixture.legacy_cache, result.stdout + result.stderr)

    def test_cleanup_does_not_claim_a_removal_it_did_not_perform(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False, cache=INVALID_CACHE)
        result = self.fixture.install("--skip-deps")
        self.assertIn("except the calibration cache", result.stdout)


class FanEnablementTests(InstallerCase):
    """Group 10: no boot-time fan service without a calibration."""

    def test_the_fan_unit_is_not_enabled_without_a_valid_cache(self):
        self.fixture.with_config()

        result = self.fixture.install("--skip-deps", "--force",
                                      "--with-fan-control")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.exists(
            "etc/systemd/system/qnap-tsx70-fancontrol.service"))
        self.assertNotIn("enable qnap-tsx70-fancontrol.service",
                         self.fixture.calls(),
                         "the fan unit was enabled with no calibration")
        self.assertIn("calibrate --yes", result.stdout)

    def test_an_invalid_cache_does_not_count_as_a_calibration(self):
        self.fixture.with_config()
        self.fixture.write(
            os.path.join(self.fixture.state_dir, "fan-calibration.json"),
            INVALID_CACHE)

        self.fixture.install("--skip-deps", "--force", "--with-fan-control")

        self.assertNotIn("enable qnap-tsx70-fancontrol.service",
                         self.fixture.calls())

    def test_a_valid_migrated_cache_allows_enablement_but_not_a_start(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False, cache=VALID_CACHE)

        result = self.fixture.install("--skip-deps", "--force",
                                      "--with-fan-control")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("enable qnap-tsx70-fancontrol.service",
                      self.fixture.calls())
        for call in self.fixture.calls():
            self.assertNotIn("start qnap-tsx70-fancontrol.service", call)
            self.assertNotIn("restart qnap-tsx70-fancontrol.service", call)

    def test_the_unit_carries_a_condition_on_the_cache(self):
        # Defence in depth behind the installer's own lifecycle, not instead
        # of it: a hand-enabled unit must not burn its start limit either.
        unit = repo_file("systemd/qnap-tsx70-fancontrol.service")
        self.assertIn(
            "ConditionPathExists=/var/lib/qnap-tsx70/fan-calibration.json",
            unit)


class FinalStatusTests(InstallerCase):
    """The closing summary describes this machine, not a default install.

    The line used to be an unconditional "Fan control is installed but NOT
    running", printed even in the one case directly above it in the script: a
    reinstall over a calibrated, active fan service, which the installer had
    just stopped to replace the binary and restore_fan_activity() had just
    started again. Telling that operator their fans are unmanaged is worse
    than saying nothing - it is the sentence that sends them to start a second
    writer by hand.
    """

    def running_fan_install(self):
        fixture = self.fixture
        fixture.with_config()
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\necho previous\n", 0o755)
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=True,
                         enabled=True)
        fixture.write(os.path.join(fixture.state_dir,
                                   "fan-calibration.json"), VALID_CACHE)
        return fixture

    def test_a_restarted_fan_service_is_reported_as_running(self):
        fixture = self.running_fan_install()

        result = fixture.install("--skip-deps", "--with-fan-control")
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("restart qnap-tsx70-fancontrol.service", fixture.calls(),
                      "the service that was active before was not restarted")
        self.assertTrue(fixture.unit_state(
            "qnap-tsx70-fancontrol.service")["active"])
        self.assertIn("installed and RUNNING", output)
        self.assertNotIn("installed but NOT running", output)
        self.assertNotIn("calibrate --yes", output,
                         "a running, calibrated service was told to calibrate")

    def test_a_fresh_install_still_says_it_is_not_running(self):
        # The control, and the common case: nothing was calibrated, nothing
        # was started, and the summary has to keep saying so.
        self.fixture.with_config()

        result = self.fixture.install("--skip-deps", "--with-fan-control")
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("installed but NOT running", output)
        self.assertNotIn("installed and RUNNING", output)
        self.assertIn("calibrate --yes", output)

    def test_an_enabled_but_stopped_service_is_told_how_to_start(self):
        # Enabled because the cache validates, not started because a first
        # start is something to watch. "Calibrate first" would be wrong here.
        self.fixture.with_config()
        self.fixture.write(os.path.join(self.fixture.state_dir,
                                        "fan-calibration.json"), VALID_CACHE)

        result = self.fixture.install("--skip-deps", "--with-fan-control")
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("installed but NOT running", output)
        self.assertIn("start at the next boot", output)
        self.assertIn("systemctl start qnap-tsx70-fancontrol", output)
        self.assertNotIn("calibrate --yes", output)

    def test_the_summary_never_turns_a_finished_install_into_a_failure(self):
        # `systemctl is-enabled` exits 1 for a disabled unit and `is-active`
        # exits 3 for an inactive one. Under `set -e`, with the ERR trap still
        # armed at that point in an earlier shape of this, either would have
        # rolled back a completed install.
        self.fixture.with_config()

        result = self.fixture.install("--skip-deps", "--with-fan-control")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("==> Done", result.stdout)
        self.assertNotIn("rolling back", result.stderr)

    def test_an_lcd_only_install_says_nothing_about_fan_control(self):
        self.fixture.with_config()

        result = self.fixture.install("--skip-deps")
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("Fan control is installed", output)


if __name__ == "__main__":
    unittest.main()


class NoMigrateTests(InstallerCase):
    """--no-migrate installs alongside a legacy tree instead of failing.

    Its contract is "do not touch a legacy saturn-* installation", and that
    covers its running services, not just its files. Stopping them was a
    silent change to the machine, and the artifacts left behind then belonged
    to nothing.
    """

    def test_no_migrate_succeeds_on_a_host_with_legacy_artifacts(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)

        result = self.fixture.install("--skip-deps", "--no-migrate")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertTrue(self.fixture.exists("usr/local/bin/saturn-lcd"),
                        "--no-migrate removed a legacy artifact")
        self.assertTrue(self.fixture.exists("etc/systemd/system/saturn-lcd.service"))
        self.assertTrue(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))

    def test_active_legacy_services_are_left_running(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        # Both legacy units up, and the LCD one not holding the panel port -
        # a machine mid-transition, where the old display daemon has been
        # pointed elsewhere but the fan controller is still doing its job.
        self.fixture.add_unit("saturn-lcd.service", active=True, enabled=True)
        self.fixture.add_unit("saturn-fancontrol.service", active=True,
                              enabled=True)

        result = self.fixture.install("--skip-deps", "--no-migrate")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        for unit in ("saturn-lcd.service", "saturn-fancontrol.service"):
            self.assertTrue(self.fixture.unit_state(unit).get("active"),
                            "--no-migrate stopped %s" % unit)
            self.assertTrue(self.fixture.unit_state(unit).get("enabled"),
                            "--no-migrate disabled %s" % unit)
            self.assertNotIn("stop %s" % unit, self.fixture.calls(),
                             "--no-migrate asked systemd to stop %s" % unit)
            self.assertNotIn("disable %s" % unit, self.fixture.calls())
        self.assertTrue(self.fixture.exists("usr/local/bin/saturn-fancontrol"))
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"))

    def test_a_live_legacy_fan_writer_is_not_stopped_either(self):
        # The quiescence gate exists to protect a binary this run deletes.
        # --no-migrate deletes none, so a legacy writer is none of its
        # business: not signalled, not waited for, not a reason to refuse.
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        self.fixture.write(self.fixture.legacy_pid, "7300\n")
        self.fixture.add_process(7300, "saturn-fancontrol",
                                 exe="/usr/local/bin/saturn-fancontrol",
                                 linger=True)

        result = self.fixture.install("--skip-deps", "--no-migrate")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertEqual(self.fixture.kill_calls(), [],
                         "--no-migrate signalled a legacy process")
        self.assertTrue(self.fixture.process_exists(7300))
        self.assertTrue(self.fixture.exists("run/saturn-fancontrol.pid"),
                        "--no-migrate removed the legacy PID file")

    def test_a_legacy_lcd_holding_the_port_fails_before_any_mutation(self):
        """The one case --no-migrate cannot have both ways.

        The legacy display daemon owns the panel and this run has promised not
        to stop it, so the new service could never open the port. Preflight
        used to call that "the planned transition" and then discover the truth
        after everything had already been replaced.
        """
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=True)   # pid 4101 holds the port
        before = self.fixture.snapshot()

        result = self.fixture.install("--skip-deps", "--no-migrate")

        self.assertNotEqual(result.returncode, 0,
                            "the install proceeded onto a port it could not get")
        self.assertIn("--no-migrate", result.stderr)
        self.assertIn("4101", result.stderr)
        self.assertNotIn("planned transition", result.stdout)
        self.assertEqual(self.fixture.snapshot(), before,
                         "preflight changed the host before refusing")
        self.assertEqual(self.fixture.mutating_calls(), [])
        self.assertTrue(self.fixture.unit_state("saturn-lcd.service")["active"])

    def test_the_same_owner_is_the_planned_transition_when_migrating(self):
        """Without --no-migrate the very same state is the normal case."""
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=True)

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("planned transition", result.stdout)


class ExcludedServiceTests(InstallerCase):
    """An LCD-only install owns no fan artifact, so it stops no fan service.

    install_files only writes bin/qnap-tsx70-fancontrol under
    --with-fan-control. Without it, the fan service keeps running the same
    binary before and after, and stopping it was pure collateral damage: the
    machine came back with fan control down and nothing saying so.
    """

    def _active_fan(self, linger=True):
        """An installed, enabled, running fan service with a live writer.

        linger=False lets the writer exit with its unit, which is what a
        --with-fan-control run needs so the in-scope stop can succeed.
        """
        fixture = self.fixture
        fixture.with_config()
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=True,
                         enabled=True, mainpid=6601, procs=[6601])
        fixture.add_process(6601, "python3",
                            exe=fixture.python_stub_versioned,
                            argv=["python3",
                                  os.path.join(fixture.bin_dir,
                                               "qnap-tsx70-fancontrol"),
                                  "run"],
                            linger=linger)
        return fixture

    def test_an_lcd_only_install_leaves_an_active_fan_service_running(self):
        self._active_fan()

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        state = self.fixture.unit_state("qnap-tsx70-fancontrol.service")
        self.assertTrue(state.get("active"),
                        "an LCD-only install left fan control stopped")
        self.assertTrue(state.get("enabled"))
        self.assertNotIn("stop qnap-tsx70-fancontrol.service",
                         self.fixture.calls(),
                         "an LCD-only install stopped a service it never touches")
        self.assertTrue(self.fixture.process_exists(6601),
                        "the running fan writer was signalled")
        self.assertTrue(self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertTrue(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))

    def test_an_lcd_only_install_is_not_blocked_by_a_live_fan_writer(self):
        # The old gate waited STOP_TIMEOUT for a writer it had no reason to
        # care about and then aborted the whole install.
        self._active_fan()

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("still running", result.stderr)
        self.assertIn("fan services left as they are", result.stdout)

    def test_with_fan_control_does_stop_it_and_puts_it_back(self):
        """In scope means in scope: the binary is replaced, so it comes down.

        It has to go back up as well. It was running when the install started,
        and a reinstall that silently leaves fan control off is the same class
        of surprise as one that stops it for no reason.
        """
        fixture = self._active_fan(linger=False)
        fixture.write(os.path.join(fixture.state_dir, "fan-calibration.json"),
                      VALID_CACHE)

        result = fixture.install("--skip-deps", "--with-fan-control", "--force")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("stop qnap-tsx70-fancontrol.service", fixture.calls())
        self.assertIn("restart qnap-tsx70-fancontrol.service", fixture.calls(),
                      "a fan service that was running was left stopped")
        self.assertTrue(
            fixture.unit_state("qnap-tsx70-fancontrol.service").get("active"))
        self.assertIn("it was active before this install", result.stdout)

    def test_a_legacy_fan_service_is_still_stopped_when_it_is_being_removed(self):
        """The flip side: scoped does not mean timid.

        An LCD-only *migration* deletes the legacy fan binaries, so the legacy
        fan unit and its writer are squarely in scope and must come down before
        the deletion, `--with-fan-control` or not.
        """
        fixture = self.fixture
        fixture.with_config()
        fixture.with_legacy(lcd_active=True, cache=VALID_CACHE)
        fixture.add_unit("saturn-fancontrol.service", active=True,
                         enabled=True, mainpid=4200, procs=[4200])
        fixture.add_process(4200, "saturn-fancontrol",
                            exe="/usr/local/bin/saturn-fancontrol")

        result = fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("stop saturn-fancontrol.service", fixture.calls(),
                      "a legacy fan unit whose binary is being deleted was "
                      "left running")
        self.assertFalse(fixture.exists("usr/local/bin/saturn-fancontrol"))
        self.assertFalse(
            fixture.exists("etc/systemd/system/saturn-fancontrol.service"))
        self.assertFalse(fixture.process_exists(4200),
                         "the legacy writer outlived its binary")

    def test_a_stopped_fan_service_without_a_calibration_is_not_started(self):
        # Restoring activity must not turn into starting something that will
        # fail at once and burn the unit's start limit.
        fixture = self._active_fan(linger=False)

        result = fixture.install("--skip-deps", "--with-fan-control", "--force")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertNotIn("restart qnap-tsx70-fancontrol.service",
                         fixture.calls())
        self.assertIn("does not validate, so it was", result.stderr)


class ExistingCacheTests(InstallerCase):
    """A calibration already in place is not overwritten by an older one."""

    def test_an_existing_valid_cache_survives_the_migration(self):
        mine = '{"version": 1, "channels": [1, 2], "fans": {}}'
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False, cache=VALID_CACHE)
        self.fixture.write(
            os.path.join(self.fixture.state_dir, "fan-calibration.json"), mine)

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.fixture.read("var/lib/qnap-tsx70/fan-calibration.json")),
            json.loads(mine), "the existing calibration was overwritten")
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"),
                        "a cache that was not migrated was deleted anyway")


class AlreadyEnabledFanTests(InstallerCase):
    """An enablement the installer cannot honour must be called out."""

    def test_an_enabled_fan_unit_without_a_valid_cache_is_flagged(self):
        self.fixture.with_config()
        self.fixture.add_unit("qnap-tsx70-fancontrol.service", enabled=True)

        result = self.fixture.install("--skip-deps", "--force",
                                      "--with-fan-control")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already enabled but", result.stderr)
        self.assertIn("systemctl disable qnap-tsx70-fancontrol", result.stderr)


class RollbackRestartTests(InstallerCase):
    """A unit that stayed active must be restarted onto the restored files."""

    def test_a_still_active_unit_is_restarted_after_the_files_come_back(self):
        self.fixture.with_config()
        self.fixture.with_current_install(active=True)
        previous = "#!/bin/sh\necho stale\n"
        self.fixture.make_start_inert("qnap-tsx70-lcd.service")

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fixture.read("usr/local/bin/qnap-tsx70-lcd"),
                         previous, "the previous binary was not restored")
        self.assertGreaterEqual(
            self.fixture.calls().count("restart qnap-tsx70-lcd.service"), 2,
            "rollback left the unit running the code it just replaced")


class CleanupQuiescenceTests(InstallerCase):
    """The gate that runs at the moment the legacy binaries are deleted.

    Checking for a fan writer once, before the install, is not enough: the
    binaries go away much later, and the window in between is long enough for
    something to start up again.
    """

    def test_a_writer_appearing_before_cleanup_stops_the_removal(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        # Appears at the daemon-reload in enable_services, i.e. after the
        # pre-install quiescence check and before legacy cleanup.
        self.fixture.spawn_process_on_daemon_reload(
            7777, "saturn-fancontrol", exe="/usr/local/bin/saturn-fancontrol",
            argv=["/usr/local/bin/saturn-fancontrol", "--run"])

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0,
                            "the legacy binaries were removed under a writer")
        self.assertIn("7777", result.stderr)
        self.assertTrue(self.fixture.exists("usr/local/bin/saturn-fancontrol"),
                        "a fan binary was deleted while its process was alive")
        self.assertTrue(self.fixture.exists("etc/systemd/system/saturn-lcd.service"))


class ActivatingUnitTests(InstallerCase):
    """`systemctl is-active` exits nonzero for a unit that is starting."""

    def test_an_activating_fan_unit_is_not_treated_as_stopped(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        self.fixture.add_unit("saturn-fancontrol.service", active=False)
        self.fixture.set_active_state("saturn-fancontrol.service", "activating")
        self.fixture.make_stop_ineffective("saturn-fancontrol.service")

        result = self.fixture.install("--skip-deps")

        self.assertNothingRemoved(result)
        self.assertIn("stop saturn-fancontrol.service", self.fixture.calls(),
                      "a unit mid-start was never asked to stop")


class UninspectablePortTests(InstallerCase):
    """A port whose owner cannot be determined is not a free port."""

    def _no_port_tools(self):
        """A PATH with neither fuser nor lsof, as on a minimal Debian install."""
        from fixtures import path_without
        os.remove(os.path.join(self.fixture.fake_bin, "fuser"))
        return {"PATH": self.fixture.fake_bin + os.pathsep
                        + path_without("fuser", "lsof")}

    def test_an_uninspectable_port_fails_preflight(self):
        self.fixture.with_config()
        env = self._no_port_tools()

        result = self.fixture.install("--skip-deps", **env)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("serial port is free", result.stdout,
                         "a port that could not be checked was called free")
        self.assertIn("cannot be determined", result.stderr)

    def test_force_does_not_cover_an_uninspectable_port(self):
        self.fixture.with_config()
        env = self._no_port_tools()

        result = self.fixture.install("--skip-deps", "--force", **env)

        self.assertNotEqual(result.returncode, 0,
                            "--force became the unknown-owner bypass")
        self.assertIn("Neither --force nor --allow-serial-owner covers this",
                      result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))

    def test_allow_serial_owner_does_not_cover_an_uninspectable_port(self):
        """The bypass that could not keep its own promise is gone.

        With neither fuser nor lsof there is nothing to check the named PID
        against: not whether it holds the port, not whether it exists, and not
        whether the port is free by the time the service opens it. Accepting
        the flag here meant printing "the port must be free before the service
        starts" and then never being able to look.
        """
        self.fixture.with_config()
        env = self._no_port_tools()

        result = self.fixture.install("--skip-deps", "--allow-serial-owner",
                                      "1234", **env)

        self.assertNotEqual(result.returncode, 0,
                            "--allow-serial-owner became the no-tools bypass")
        self.assertIn("Neither --force nor --allow-serial-owner covers this",
                      result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"),
                         "an unverifiable port still got an install")
        self.assertEqual(self.fixture.mutating_calls(), [],
                         "preflight mutated the host before refusing")

    def test_the_refusal_names_a_tool_to_install(self):
        # "Install psmisc, or take responsibility for it" sent people to the
        # flag. There is no responsibility to take here, only a missing tool.
        self.fixture.with_config()
        env = self._no_port_tools()

        result = self.fixture.install("--skip-deps", **env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("psmisc", result.stderr)
        self.assertIn("lsof", result.stderr)
        self.assertNotIn("serial port is free", result.stdout,
                         "a port that could not be checked was called free")

    def test_no_ownership_is_claimed_for_a_pid_nothing_could_check(self):
        """The reassurance must not be printed where it cannot be true.

        With no inspection tool the installer used to accept the flag and say
        "the port must be free before the service starts" - a promise it had
        no way to keep. Nothing may be asserted about a PID on a host where
        the port cannot be read.
        """
        self.fixture.with_config()
        env = self._no_port_tools()

        result = self.fixture.install("--skip-deps", "--allow-serial-owner",
                                      "1234", **env)
        output = result.stdout + result.stderr

        self.assertNotIn("as the serial port owner on your word", output)
        self.assertNotIn("serial port owner accepted", output)
        self.assertNotIn("1234", output.replace(self.fixture.root, ""))

    def test_a_tool_that_disappears_mid_run_still_stops_the_service_start(self):
        """The second guard, on the path preflight cannot cover.

        Preflight refuses a host with no inspection tool, so the check before
        the service is started is only reachable when the tool goes away in
        between - a package removed or upgraded mid-install. It used to accept
        --allow-serial-owner there and start the service on a port it had not
        looked at.
        """
        self.fixture.with_config()
        env = self.fixture.make_port_tools_vanish_after_one_use()

        result = self.fixture.install("--skip-deps", "--allow-serial-owner",
                                      "1234", **env)

        self.assertIn("serial port is free", result.stdout,
                      "preflight never got its one answer out of fuser")
        self.assertNotEqual(result.returncode, 0,
                            "the service was started on an unverified port")
        self.assertIn("cannot confirm", result.stderr)
        self.assertIn("not started on an unverified port", result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))

    def test_the_flag_is_still_accepted_where_the_port_can_be_read(self):
        """Removing the no-tools bypass must not remove the flag itself."""
        self.fixture.with_config()
        self.fixture.add_process(9400, "socat", exe="/usr/bin/socat",
                                 argv=["socat", "-", "/dev/ttyS1"])
        self.fixture.hold_port(9400)

        result = self.fixture.install("--skip-deps", "--allow-serial-owner",
                                      "9400")

        self.assertIn("preflight passed", result.stdout,
                      "the documented escape hatch stopped working")
        self.assertIn("serial port owner accepted", result.stdout)


class FanCommandClassificationTests(InstallerCase):
    """Which argv is a fan writer, decided structurally rather than by words.

    The first version of the read-only test asked whether *any* argv token
    equalled `status`, `validate-cache`, `--version` or `--help`. So
    `qnap-tsx70-fancontrol run --sensor status` - a live control loop whose
    temperature source happens to be called status - was classified read-only,
    the quiescence gate passed, and the legacy binaries were deleted under it.
    """

    def _legacy_with_writer(self, argv, linger=True, pid=7500):
        """A migration blocked (or not) by one process running `argv`."""
        fixture = self.fixture
        fixture.with_config()
        fixture.with_legacy(lcd_active=False)
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit("qnap-tsx70-fancontrol.service", active=False)
        fixture.add_process(pid, "python3", exe=fixture.python_stub_versioned,
                            argv=["python3",
                                  os.path.join(fixture.bin_dir,
                                               "qnap-tsx70-fancontrol")]
                                 + list(argv),
                            linger=linger)
        return fixture

    def assertBlocked(self, result, pid=7500):
        self.assertNothingRemoved(result)
        self.assertIn(str(pid), result.stderr)
        self.assertIn("still running", result.stderr)
        self.assertTrue(self.fixture.exists("usr/local/bin/saturn-fancontrol"),
                        "a legacy fan binary went while a writer was alive")
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"))

    def test_a_run_whose_sensor_is_called_status_is_still_a_writer(self):
        self._legacy_with_writer(["run", "--sensor", "status"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertBlocked(result)

    def test_the_same_holds_for_the_equals_form(self):
        self._legacy_with_writer(["run", "--sensor=status"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertBlocked(result)

    def test_a_cache_path_that_ends_in_a_read_only_word_is_no_defence(self):
        self._legacy_with_writer(["run", "--cache", "/var/tmp/status"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertBlocked(result)

    def test_calibrate_is_a_writer(self):
        self._legacy_with_writer(["calibrate", "--yes", "--sensor", "status"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertBlocked(result)

    def test_safe_state_is_a_writer(self):
        self._legacy_with_writer(["safe-state", "--cache", "status"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertBlocked(result)

    def test_an_invocation_with_no_command_at_all_is_treated_as_a_writer(self):
        # Fail closed: nothing here says what it is doing.
        self._legacy_with_writer([])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertBlocked(result)

    def test_an_unknown_command_is_treated_as_a_writer(self):
        # A subcommand a later version adds must block until someone
        # classifies it, not default to harmless.
        self._legacy_with_writer(["tune", "--sensor", "status"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertBlocked(result)

    def test_a_genuine_status_command_does_not_block_the_migration(self):
        # The other half of the bargain: a read-only command left open in
        # another terminal must not stall the install for STOP_TIMEOUT and
        # then abort. This is what the word test got right.
        self._legacy_with_writer(["status"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/saturn-fancontrol"),
                         "a read-only process blocked the cleanup")
        self.assertTrue(self.fixture.process_exists(7500),
                        "the read-only process was signalled")

    def test_a_status_with_options_of_its_own_is_still_read_only(self):
        self._legacy_with_writer(["status", "--json", "--cache", "/x"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/saturn-fancontrol"))

    def test_validate_cache_does_not_block_the_migration(self):
        # The installer runs this itself to decide whether a cache migrates.
        self._legacy_with_writer(["validate-cache", "--cache",
                                  "/etc/saturn-fan-cache.json"])

        result = self.fixture.install("--skip-deps", "--with-fan-control",
                                      "--force")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(self.fixture.exists("usr/local/bin/saturn-fancontrol"))


class FanHandbackTests(InstallerCase):
    """No fan recovery artifact is deleted or replaced on an unproven handback.

    "The unit is inactive" and "no fan process is running" are both equally
    true of a machine whose fan is parked at a calibration stall duty with
    pwmN_enable still reading 1. Neither says anything about the PWM
    registers. Only a write followed by a readback does, which is what
    `safe-state` exits 0 for - and until this pass, the installer deleted the
    legacy fan binary, and replaced the current one, without ever asking.

    The chip is a disposable directory and the program driving it is this
    repository's own source with geteuid faked. No /sys path is touched.
    """

    FAN_ARTIFACTS = ("usr/local/bin/saturn-fancontrol",
                     "usr/local/bin/saturn-fan-calibrate",
                     "etc/systemd/system/saturn-fancontrol.service")

    def legacy_with_fan(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False)
        self.fixture.write(
            os.path.join(self.fixture.bin_dir, "saturn-fan-calibrate"),
            "#!/bin/sh\n", 0o755)
        return self.fixture

    def assertFanArtifactsKept(self, result):
        self.assertNotEqual(result.returncode, 0,
                            "the installer should have aborted:\n%s"
                            % result.stdout)
        for path in self.FAN_ARTIFACTS:
            self.assertTrue(self.fixture.exists(path),
                            "%s was removed although the fans were never "
                            "confirmed back" % path)
        self.assertIn("automatic", result.stderr + result.stdout)

    # -- the control ---------------------------------------------------------
    def test_a_verified_handback_lets_the_migration_finish(self):
        self.legacy_with_fan()
        self.fixture.with_fan_controller(enable=1)

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("safe-state", " ".join(self.fixture.fan_commands()))
        self.assertIn("automatic fan mode verified", result.stdout)
        for path in self.FAN_ARTIFACTS:
            self.assertFalse(self.fixture.exists(path),
                             "%s survived a completed migration" % path)
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "2")

    # -- the failure matrix --------------------------------------------------
    def test_a_chip_that_stays_in_manual_mode_keeps_every_fan_artifact(self):
        self.legacy_with_fan()
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.fixture.install("--skip-deps")

        self.assertFanArtifactsKept(result)
        self.assertIn("still in mode 1", result.stderr)

    def test_a_controller_that_is_not_there_keeps_every_fan_artifact(self):
        # The driver is not loaded, so there is nothing to ask. That is not
        # permission to delete the program that would have asked.
        self.legacy_with_fan()
        self.fixture.without_fan_controller()

        result = self.fixture.install("--skip-deps")

        self.assertFanArtifactsKept(result)

    def test_an_unreadable_mode_register_keeps_every_fan_artifact(self):
        self.legacy_with_fan()
        self.fixture.with_fan_controller(enable="",
                                         ignore_writes=["pwm3_enable"])

        result = self.fixture.install("--skip-deps")

        self.assertFanArtifactsKept(result)

    def test_a_refused_register_write_keeps_every_fan_artifact(self):
        self.legacy_with_fan()
        self.fixture.with_fan_controller(enable=1,
                                         fail_writes=["pwm3_enable"])

        result = self.fixture.install("--skip-deps")

        self.assertFanArtifactsKept(result)

    def test_a_chip_with_no_controllable_channel_keeps_every_fan_artifact(self):
        self.legacy_with_fan()
        self.fixture.with_fan_controller(controllable=False)

        result = self.fixture.install("--skip-deps")

        self.assertFanArtifactsKept(result)
        self.assertIn("no controllable channel", result.stderr)

    def test_a_failed_handback_rolls_the_install_back(self):
        # Not merely "the legacy tree survives": the run is a transaction, so
        # a refusal here leaves the machine as it was found.
        self.legacy_with_fan()
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.fixture.install("--skip-deps")

        self.assertFanArtifactsKept(result)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"),
                         "the install went ahead after refusing")
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"))

    # -- scope ---------------------------------------------------------------
    def test_an_lcd_only_install_never_touches_the_fan_controller(self):
        # Nothing fan-related is being replaced or removed, so there is
        # nothing to prove and no business writing to a fan controller.
        self.fixture.with_config()

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.fan_commands(), [],
                         "an LCD-only install wrote to the fan controller")
        self.assertIn("the fan controller is left alone", result.stdout)

    def test_an_lcd_only_install_is_unaffected_by_a_broken_controller(self):
        # The proof that the scope decision is real: the same install on a
        # machine whose chip refuses everything still succeeds.
        self.fixture.with_config()
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.fan_commands(), [])

    def test_migrating_a_legacy_tree_with_no_fan_artifacts_touches_nothing(self):
        # A legacy install that only ever had the display. There is no fan
        # binary to delete, so no fan register is written.
        self.fixture.with_config()
        self.fixture.write(os.path.join(self.fixture.bin_dir, "saturn-lcd"),
                           "#!/bin/sh\n", 0o755)
        self.fixture.add_unit("saturn-lcd.service", active=False, enabled=True)
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.fan_commands(), [])
        self.assertFalse(self.fixture.exists("usr/local/bin/saturn-lcd"))

    def test_replacing_the_installed_fan_binary_verifies_first(self):
        # --with-fan-control over an existing install replaces the binary and
        # the unit. Those are the recovery artifacts, so the same rule applies
        # even though nothing is being migrated.
        self.fixture.with_config()
        self.fixture.with_current_install(active=False)
        self.fixture.write(
            os.path.join(self.fixture.bin_dir, "qnap-tsx70-fancontrol"),
            "#!/bin/sh\necho stale\n", 0o755)
        self.fixture.add_unit("qnap-tsx70-fancontrol.service", active=False)
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.fixture.install("--skip-deps", "--with-fan-control")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.read("usr/local/bin/qnap-tsx70-fancontrol"),
                         "#!/bin/sh\necho stale\n",
                         "the recovery binary was replaced without a verified "
                         "handback")

    def test_a_first_fan_control_install_needs_no_handback(self):
        # Nothing is being replaced: this run *adds* the recovery mechanism.
        # Demanding a verified handback here would mean the fan binary could
        # never be installed on a machine that needs it most.
        self.fixture.with_config()
        self.fixture.without_fan_controller()

        result = self.fixture.install("--skip-deps", "--force",
                                      "--with-fan-control")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertEqual(self.fixture.fan_commands(), [])
        self.assertTrue(
            self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))

    # -- ordering ------------------------------------------------------------
    def test_the_handback_is_only_asked_for_once_nothing_is_writing(self):
        # Asking the chip to take the fans back while a writer is still
        # driving them is answered by whichever of the two wrote last, so the
        # quiescence gate has to come first and refuse on its own.
        self.legacy_with_fan()
        self.fixture.add_unit("saturn-fancontrol.service", active=True,
                              enabled=True)
        self.fixture.make_stop_ineffective("saturn-fancontrol.service")

        result = self.fixture.install("--skip-deps")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fixture.fan_commands(), [],
                         "the chip was written to while a writer was alive")
        for path in self.FAN_ARTIFACTS:
            self.assertTrue(self.fixture.exists(path))

    def test_a_dry_run_previews_the_gate_and_writes_no_register(self):
        self.legacy_with_fan()
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])
        before = self.fixture.snapshot()

        result = self.fixture.install("--skip-deps", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry-run]", result.stdout)
        self.assertIn("gated on its exit status", result.stdout)
        self.assertEqual(self.fixture.fan_commands(), [],
                         "a dry run wrote to the fan controller")
        self.assertEqual(self.fixture.snapshot(), before,
                         "the dry run changed the fake root")


class CacheReportingTests(InstallerCase):
    """Two different reasons to keep a legacy cache, reported as two."""

    def test_a_superseded_cache_is_not_called_invalid(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False, cache=VALID_CACHE)
        self.fixture.write(
            os.path.join(self.fixture.state_dir, "fan-calibration.json"),
            '{"version": 1, "channels": [1, 2], "fans": {}}')

        result = self.fixture.install("--skip-deps")
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("A newer calibration was already in place", output)
        self.assertNotIn("did NOT validate", output,
                         "a valid legacy cache was reported as invalid")

    def test_an_invalid_cache_is_still_called_invalid(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=False, cache=INVALID_CACHE)

        result = self.fixture.install("--skip-deps")

        self.assertIn("did NOT validate", result.stdout + result.stderr)
