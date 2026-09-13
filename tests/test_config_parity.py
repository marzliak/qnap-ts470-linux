"""Group 11: the runtime and the shell scripts must read one configuration.

The installer and the diagnostics used to parse the file with sed, which kept
the quotes and the inline comment the Python runtime strips. A valid
`serial_port = "/dev/ttyS1"  # panel` was therefore a path that did not exist.
"""

import os
import subprocess
import tempfile
import unittest

from helpers import REPO_ROOT, load_lcd

CASES = {
    "plain": ("serial_port = /dev/ttyS1\n", "/dev/ttyS1"),
    "double quoted": ('serial_port = "/dev/ttyS1"\n', "/dev/ttyS1"),
    "single quoted": ("serial_port = '/dev/ttyS1'\n", "/dev/ttyS1"),
    "inline comment": ("serial_port = /dev/ttyS1  # the panel\n", "/dev/ttyS1"),
    "quoted and commented": ('serial_port = "/dev/ttyS1" # panel\n', "/dev/ttyS1"),
    "no spaces": ("serial_port=/dev/ttyS1\n", "/dev/ttyS1"),
    "duplicate keys": ("serial_port = /dev/ttyS0\nserial_port = /dev/ttyS1\n",
                       "/dev/ttyS1"),
    "leading whitespace": ("   serial_port =    /dev/ttyS1   \n", "/dev/ttyS1"),
}


def write_config(case, body):
    handle = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False,
                                         encoding="utf-8")
    handle.write(body)
    handle.close()
    case.addCleanup(os.unlink, handle.name)
    return handle.name


class PrintConfigTests(unittest.TestCase):
    def test_every_form_yields_the_same_effective_value(self):
        for name, (body, expected) in CASES.items():
            with self.subTest(form=name):
                path = write_config(self, body)
                result = subprocess.run(
                    ["python3", "bin/qnap-tsx70-lcd", "--config", path,
                     "--print-config", "serial_port"],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_the_runtime_agrees_with_the_printed_value(self):
        lcd = load_lcd()
        for name, (body, expected) in CASES.items():
            with self.subTest(form=name):
                self.assertEqual(lcd.parse_config(body)["serial_port"], expected)

    def test_a_missing_file_yields_the_built_in_default(self):
        result = subprocess.run(
            ["python3", "bin/qnap-tsx70-lcd", "--config", "/nonexistent.conf",
             "--print-config", "serial_port"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/dev/ttyS1")

    def test_an_unknown_key_is_an_error(self):
        result = subprocess.run(
            ["python3", "bin/qnap-tsx70-lcd", "--print-config", "nope"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown configuration key", result.stderr)

    def test_a_malformed_value_is_an_error_not_a_silent_default(self):
        path = write_config(self, "page_interval = not-a-number\n")
        result = subprocess.run(
            ["python3", "bin/qnap-tsx70-lcd", "--config", path,
             "--print-config", "page_interval"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not a number", result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_a_key_written_without_an_equals_sign_is_an_error(self):
        path = write_config(self, "serial_port /dev/ttyS1\n")
        result = subprocess.run(
            ["python3", "bin/qnap-tsx70-lcd", "--config", path,
             "--print-config", "serial_port"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a key = value pair", result.stderr)

    def test_an_unrelated_typo_does_not_block_an_unrelated_key(self):
        path = write_config(self, "serial_port = /dev/ttyS3\nbogus = 1\n")
        result = subprocess.run(
            ["python3", "bin/qnap-tsx70-lcd", "--config", path,
             "--print-config", "serial_port"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/dev/ttyS3")

    def test_booleans_and_numbers_are_shell_readable(self):
        path = write_config(self, "smart_enabled = no\npage_interval = 7\n")
        for key, expected in (("smart_enabled", "false"), ("page_interval", "7")):
            with self.subTest(key=key):
                result = subprocess.run(
                    ["python3", "bin/qnap-tsx70-lcd", "--config", path,
                     "--print-config", key],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)


class ScriptParityTests(unittest.TestCase):
    """install.sh and diagnose.sh must report what the runtime would use."""

    def _diagnose_port(self, body):
        path = write_config(self, body)
        result = subprocess.run(["bash", "scripts/diagnose.sh"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=180,
                                env=dict(os.environ, QNAP_TSX70_CONF=path))
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith("configured port:"):
                return line.split(":", 1)[1].strip()
        self.fail("diagnose.sh printed no configured port:\n%s" % result.stdout)

    def test_diagnose_reports_the_effective_port_for_every_form(self):
        for name, (body, expected) in CASES.items():
            with self.subTest(form=name):
                self.assertEqual(self._diagnose_port(body), expected)

    def test_the_installer_accepts_a_quoted_and_commented_port(self):
        from fixtures import ShellFixture
        fixture = ShellFixture(self)
        fixture.write(fixture.conf_file,
                      'serial_port = "%s"   # the front panel\n' % fixture.port)

        result = fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("serial port %s present" % fixture.port, result.stdout)


if __name__ == "__main__":
    unittest.main()
