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
        # left a half-migrated system behind with no rollback at all.
        self._rich_prior_state()
        stuck = os.path.join(self.fixture.unit_dir, "saturn-fan.service")
        os.makedirs(stuck, exist_ok=True)
        self.fixture.add_unit("saturn-fan.service", with_file=False)
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


if __name__ == "__main__":
    unittest.main()


class NoMigrateTests(InstallerCase):
    """--no-migrate installs alongside a legacy tree instead of failing."""

    def test_no_migrate_succeeds_on_a_host_with_legacy_artifacts(self):
        self.fixture.with_config()
        self.fixture.with_legacy(lcd_active=True)

        result = self.fixture.install("--skip-deps", "--no-migrate")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertTrue(self.fixture.exists("usr/local/bin/saturn-lcd"),
                        "--no-migrate removed a legacy artifact")
        self.assertTrue(self.fixture.exists("etc/systemd/system/saturn-lcd.service"))
        self.assertTrue(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))


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
