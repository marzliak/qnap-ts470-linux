"""Shell script checks: syntax, and a real dry-run of the installer.

The dry-run is an integration test, not a style check. It exists because
`set -euo pipefail` plus a `sed` that exits 2 on a missing config file made the
installer abort silently, with status 2, before touching anything - on exactly
the first-install path nobody had exercised. Syntax checking cannot find that.
"""

import os
import subprocess
import unittest

from helpers import REPO_ROOT

SCRIPTS = ["scripts/install.sh", "scripts/uninstall.sh", "scripts/diagnose.sh"]


def run(argv, **kwargs):
    return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True,
                          timeout=180, **kwargs)


class SyntaxTests(unittest.TestCase):
    def test_bash_n_accepts_every_script(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result = run(["bash", "-n", script])
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_scripts_declare_bash_not_sh(self):
        # They use arrays and [[ ]]-free bashisms; a /bin/sh shebang would be
        # a portability claim the code does not honour.
        for script in SCRIPTS:
            with open(os.path.join(REPO_ROOT, script), encoding="utf-8") as fh:
                self.assertIn("bash", fh.readline())

    def test_help_exits_cleanly(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result = run(["bash", script, "--help"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage:", result.stdout)

    def test_unknown_option_is_rejected(self):
        for script in ("scripts/install.sh", "scripts/uninstall.sh"):
            with self.subTest(script=script):
                result = run(["bash", script, "--not-a-real-option"])
                self.assertNotEqual(result.returncode, 0)


class InstallerDryRunTests(unittest.TestCase):
    """--force is used so the run does not depend on the panel being present."""

    @classmethod
    def setUpClass(cls):
        import shutil
        if not shutil.which("systemctl"):
            raise unittest.SkipTest("systemd is required by the installer")
        cls.result = run(["bash", "scripts/install.sh", "--dry-run",
                          "--skip-deps", "--force"])

    def test_dry_run_completes_successfully(self):
        self.assertEqual(self.result.returncode, 0,
                         "installer exited %d\nstdout:\n%s\nstderr:\n%s"
                         % (self.result.returncode, self.result.stdout,
                            self.result.stderr))

    def test_dry_run_reaches_the_end(self):
        self.assertIn("Done", self.result.stdout,
                      "installer stopped early:\n%s" % self.result.stdout)

    def test_dry_run_covers_every_stage(self):
        for marker in ("Preflight", "Installing files", "Enabling services",
                       "Validation", "Done"):
            self.assertIn(marker, self.result.stdout, marker)

    def test_dry_run_announces_its_mutations_without_performing_them(self):
        for expected in ("/usr/local/bin/qnap-tsx70-lcd",
                         "/etc/qnap-tsx70-lcd.conf",
                         "qnap-tsx70-lcd.service",
                         "systemctl daemon-reload",
                         "systemctl enable qnap-tsx70-lcd.service"):
            self.assertIn(expected, self.result.stdout, expected)
        self.assertIn("[dry-run]", self.result.stdout)

    def test_dry_run_changes_nothing_on_disk(self):
        # Compare before and after rather than asserting absence: on a machine
        # where the project is genuinely installed, absence is the wrong test.
        targets = ("/usr/local/bin/qnap-tsx70-lcd",
                   "/etc/qnap-tsx70-lcd.conf",
                   "/etc/systemd/system/qnap-tsx70-lcd.service")

        def snapshot():
            state = {}
            for path in targets:
                try:
                    info = os.stat(path)
                    state[path] = (info.st_ino, info.st_mtime, info.st_size)
                except OSError:
                    state[path] = None
            return state

        before = snapshot()
        result = run(["bash", "scripts/install.sh", "--dry-run", "--skip-deps",
                      "--force"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(), before, "the dry run modified the system")

    def test_lcd_only_is_the_default(self):
        # Asserted behaviourally rather than by absence of the word: the
        # transaction manifest legitimately records the fan paths so that a
        # rollback knows they were absent, which is not the same as
        # installing, enabling or starting anything.
        self.assertIn("mode:       LCD only", self.result.stdout)
        for action in ("install -m 0755 %s/bin/qnap-tsx70-fancontrol" % REPO_ROOT,
                       "systemctl enable qnap-tsx70-fancontrol.service",
                       "systemctl start qnap-tsx70-fancontrol.service",
                       "fan control (experimental)"):
            self.assertNotIn(action, self.result.stdout, action)

    def test_fan_control_dry_run_is_opt_in_and_never_started(self):
        result = run(["bash", "scripts/install.sh", "--dry-run", "--skip-deps",
                      "--force", "--with-fan-control"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fan control (experimental)", result.stdout)
        self.assertIn("NOT started", result.stdout)
        # Calibration stops the fan, so the installer must never trigger it.
        self.assertNotIn("[dry-run] qnap-tsx70-fancontrol calibrate",
                         result.stdout)


class UninstallerDryRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        if not shutil.which("systemctl"):
            raise unittest.SkipTest("systemd is required by the uninstaller")
        cls.result = run(["bash", "scripts/uninstall.sh", "--dry-run"])

    def test_dry_run_completes_successfully(self):
        self.assertEqual(self.result.returncode, 0,
                         self.result.stdout + self.result.stderr)

    def test_dry_run_reaches_the_end(self):
        self.assertIn("Done", self.result.stdout)

    def test_state_is_preserved_without_purge(self):
        self.assertIn("Kept", self.result.stdout)
        self.assertNotIn("Purging", self.result.stdout)

    def test_purge_is_announced_when_requested(self):
        result = run(["bash", "scripts/uninstall.sh", "--dry-run", "--purge"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Purging", result.stdout)


class DiagnoseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run(["bash", "scripts/diagnose.sh"])

    def test_diagnose_runs(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)

    def test_bundle_has_the_expected_sections(self):
        for section in ("System", "Chassis", "Serial ports", "hwmon sensors",
                        "Fan controller", "Configuration", "Disks", "Services"):
            self.assertIn("===== %s =====" % section, self.result.stdout)

    def test_bundle_contains_no_private_address_or_mac(self):
        import re
        body = self.result.stdout
        self.assertIsNone(
            re.search(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", body),
            "a MAC address survived redaction")
        self.assertIsNone(
            re.search(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))"
                      r"\.\d{1,3}\.\d{1,3}\b", body),
            "a private IPv4 address survived redaction")
        self.assertIsNone(
            re.search(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", body),
            "a UUID survived redaction")

    def test_bundle_does_not_leak_this_machines_hostname(self):
        import socket
        host = socket.gethostname().split(".")[0]
        if len(host) < 2:
            self.skipTest("hostname too short to search for meaningfully")
        import re
        self.assertIsNone(re.search(r"\b%s\b" % re.escape(host),
                                    self.result.stdout, re.IGNORECASE),
                          "the hostname survived redaction")


if __name__ == "__main__":
    unittest.main()


class RollbackReachabilityTests(unittest.TestCase):
    """A failure inside install_files() must reach rollback().

    Bash does not inherit an ERR trap into shell functions unless `errtrace`
    is set, so `set -euo pipefail` silently disabled the documented automatic
    rollback for exactly the two functions that perform the install.
    """

    PROBE = "scripts/.install-rollback-probe.sh"

    def _build_probe(self, shell_options="set -Eeuo pipefail"):
        with open(os.path.join(REPO_ROOT, "scripts/install.sh"),
                  encoding="utf-8") as fh:
            src = fh.read()
        probe = src.replace("set -Eeuo pipefail", shell_options)
        # Inject a failure at the first statement of install_files.
        marker = ('    step "Installing files"\n'
                  '    txn_mkdir "$STATE_DIR" 0755')
        self.assertIn(marker, probe, "the probe's injection point moved")
        probe = probe.replace(marker, "    false\n" + marker, 1)
        # Pretend a legacy install exists, without touching the real system.
        for name, stub in (
            ("detect_legacy() {", "detect_legacy() { return 0\n"),
            ("txn_begin() {",
             'txn_begin() { BACKUP_DIR="$(mktemp -d)"; TXN_DIR="$BACKUP_DIR/txn"; return 0\n'),
            ("txn_snapshot_all() {", "txn_snapshot_all() { return 0\n"),
            ("make_backup() {", "make_backup() { return 0\n"),
            ("stop_current_services() {", "stop_current_services() { return 0\n"),
            ("verify_port_released() {", "verify_port_released() { return 0\n"),
            ("migrate_cache() {", "migrate_cache() { return 0\n"),
            ("rollback() {", 'rollback() { echo "ROLLBACK RAN"; return 0\n'),
        ):
            probe = probe.replace(name, stub, 1)
        path = os.path.join(REPO_ROOT, self.PROBE)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(probe)
        os.chmod(path, 0o755)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return self.PROBE

    def _run_probe(self, shell_options="set -Eeuo pipefail"):
        import shutil
        if not shutil.which("systemctl"):
            self.skipTest("systemd is required by the installer")
        probe = self._build_probe(shell_options)
        result = run(["bash", probe, "--dry-run", "--skip-deps", "--force"])
        return result, result.stdout + result.stderr

    def test_failure_inside_install_files_triggers_rollback(self):
        result, output = self._run_probe()
        self.assertIn("ROLLBACK RAN", output,
                      "rollback did not run:\n%s" % output)
        self.assertNotEqual(result.returncode, 0,
                            "a failed install must exit nonzero")

    def test_errtrace_is_what_makes_that_work(self):
        # Guards the fix itself: drop -E and the rollback becomes unreachable.
        _, output = self._run_probe(shell_options="set -euo pipefail")
        self.assertNotIn("ROLLBACK RAN", output,
                         "expected the no-errtrace build to skip rollback; if "
                         "this now passes, the rollback no longer depends on "
                         "the ERR trap and this test can go")

    def test_installer_enables_errtrace(self):
        with open(os.path.join(REPO_ROOT, "scripts/install.sh"),
                  encoding="utf-8") as fh:
            self.assertIn("set -Eeuo pipefail", fh.read())


class IdempotencyTests(unittest.TestCase):
    def test_uninstall_twice_is_a_no_op_and_still_succeeds(self):
        import shutil
        if not shutil.which("systemctl"):
            self.skipTest("systemd is required by the uninstaller")
        first = run(["bash", "scripts/uninstall.sh", "--dry-run"])
        second = run(["bash", "scripts/uninstall.sh", "--dry-run"])
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_install_dry_run_is_repeatable(self):
        import shutil
        if not shutil.which("systemctl"):
            self.skipTest("systemd is required by the installer")
        first = run(["bash", "scripts/install.sh", "--dry-run", "--skip-deps",
                     "--force"])
        second = run(["bash", "scripts/install.sh", "--dry-run", "--skip-deps",
                      "--force"])
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
