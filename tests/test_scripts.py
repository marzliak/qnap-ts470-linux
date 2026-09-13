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
        for path in ("/usr/local/bin/qnap-tsx70-lcd",
                     "/etc/systemd/system/qnap-tsx70-lcd.service"):
            self.assertFalse(os.path.exists(path),
                             "dry run created %s" % path)

    def test_lcd_only_is_the_default(self):
        self.assertIn("mode:       LCD only", self.result.stdout)
        self.assertNotIn("qnap-tsx70-fancontrol", self.result.stdout)

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
