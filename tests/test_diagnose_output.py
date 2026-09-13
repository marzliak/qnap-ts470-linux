"""Group 13: writing the bundle to a file is a transaction or it is a failure.

The old code redirected straight onto the target, ignored the exit status of
the pipeline, and printed "Wrote redacted bundle to ..." unconditionally. A
bad path therefore produced a success message, exit status 0, and no file.
"""

import glob
import os
import stat
import subprocess
import tempfile
import unittest

from helpers import REPO_ROOT

SUCCESS = "Wrote redacted bundle to"


def diagnose(*args, **env):
    return subprocess.run(["bash", "scripts/diagnose.sh"] + list(args),
                          cwd=REPO_ROOT, capture_output=True, text=True,
                          timeout=180, env=dict(os.environ, **env))


class OutputTransactionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qnap-diagnose-")
        self.addCleanup(self._cleanup)
        self.target = os.path.join(self.dir, "bundle.txt")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def leftovers(self):
        return glob.glob(os.path.join(self.dir, ".qnap-tsx70-diagnose.*"))

    def assertFailedCleanly(self, result):
        self.assertNotEqual(result.returncode, 0,
                            "expected a nonzero exit:\n%s" % result.stderr)
        self.assertNotIn(SUCCESS, result.stderr + result.stdout,
                         "success was announced for a run that failed")
        self.assertEqual(self.leftovers(), [],
                         "a partial temporary file was left behind")

    def test_a_successful_write_is_0600_and_complete(self):
        result = diagnose("-o", self.target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(SUCCESS, result.stderr)
        self.assertIn("Review it", result.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)
        with open(self.target, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("qnap-tsx70 diagnostic bundle", body)
        self.assertIn("end of bundle", body)
        self.assertEqual(self.leftovers(), [])

    def test_a_missing_output_argument_is_rejected(self):
        result = diagnose("-o")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SUCCESS, result.stderr + result.stdout)
        self.assertIn("requires a file argument", result.stderr)

    def test_an_output_argument_that_is_another_option_is_rejected(self):
        result = diagnose("-o", "--with-smart")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a file argument", result.stderr)

    def test_an_empty_output_argument_is_rejected(self):
        result = diagnose("--output=")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SUCCESS, result.stderr + result.stdout)

    def test_a_nonexistent_parent_directory_fails(self):
        result = diagnose("-o", os.path.join(self.dir, "missing/bundle.txt"))
        self.assertFailedCleanly(result)
        self.assertIn("output directory does not exist", result.stderr)

    def test_a_directory_as_the_output_path_fails(self):
        before = stat.S_IMODE(os.stat(self.dir).st_mode)

        result = diagnose("-o", self.dir)

        self.assertFailedCleanly(result)
        self.assertIn("is a directory", result.stderr)
        self.assertTrue(os.path.isdir(self.dir), "the directory was clobbered")
        # The old code ran an unconditional `chmod 0600` on the target, which
        # turned a 0755 directory into a 0600 one on the way to failing.
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), before,
                         "the directory's mode was changed by a failed write")

    def test_an_unwritable_destination_fails(self):
        # procfs refuses file creation for root too, so this is deterministic
        # whoever runs the suite.
        result = diagnose("-o", "/proc/self/fdinfo/bundle.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SUCCESS, result.stderr + result.stdout)
        self.assertIn("cannot create a temporary file", result.stderr)

    def test_a_permission_denied_directory_fails(self):
        if os.geteuid() == 0:
            self.skipTest("mode bits do not stop root")
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        self.assertFailedCleanly(diagnose("-o", self.target))

    def test_a_failing_redactor_fails_the_run_and_writes_nothing(self):
        broken = os.path.join(self.dir, "broken-redactor.py")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("import sys\nsys.exit(9)\n")

        result = diagnose("-o", self.target, QNAP_TSX70_REDACT=broken)

        self.assertFailedCleanly(result)
        self.assertFalse(os.path.exists(self.target),
                         "an unredacted or partial bundle was written")

    def test_a_missing_redactor_refuses_to_produce_a_bundle(self):
        result = diagnose(QNAP_TSX70_REDACT=os.path.join(self.dir, "absent.py"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to produce an unredacted bundle", result.stderr)

    def test_an_existing_target_is_replaced_atomically(self):
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("previous contents\n")
        os.chmod(self.target, 0o644)

        result = diagnose("-o", self.target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)
        with open(self.target, encoding="utf-8") as fh:
            self.assertNotIn("previous contents", fh.read())

    def test_an_unknown_option_is_rejected(self):
        result = diagnose("--not-a-real-option")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown option", result.stderr)


class BundlePrivacyTests(unittest.TestCase):
    """The written bundle gets the same redaction as the streamed one."""

    def test_the_written_bundle_is_redacted(self):
        directory = tempfile.mkdtemp(prefix="qnap-diagnose-")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory,
                                                            ignore_errors=True))
        target = os.path.join(directory, "bundle.txt")
        conf = os.path.join(directory, "lcd.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            # A config the collector prints back verbatim.
            fh.write("serial_port = /dev/ttyS1\nnet_interface = eth0\n")

        result = diagnose("-o", target, QNAP_TSX70_CONF=conf)

        self.assertEqual(result.returncode, 0, result.stderr)
        with open(target, encoding="utf-8") as fh:
            body = fh.read()
        import re
        self.assertIsNone(
            re.search(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", body),
            "a MAC address survived redaction")
        self.assertIsNone(
            re.search(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))"
                      r"\.\d{1,3}\.\d{1,3}\b", body),
            "a private IPv4 address survived redaction")


if __name__ == "__main__":
    unittest.main()
