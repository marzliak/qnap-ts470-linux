"""The fan-only uninstall in docs/FAN_CONTROL.md, executed rather than read.

The guide used to answer "how do I remove just fan control?" with

    sudo systemctl disable --now qnap-tsx70-fancontrol
    sudo rm /etc/systemd/system/qnap-tsx70-fancontrol.service
    sudo rm /usr/local/bin/qnap-tsx70-fancontrol

which is the exact sequence scripts/uninstall.sh exists to refuse: `disable
--now` reports success for a unit that is still shutting down, it says nothing
about a writer that outlived its unit, and it cannot tell anyone whether the
chip took the fans back - and then the `rm` removes the only executable that
could have found out. So the guide now gives the supported command, and these
tests run whatever command it gives against a fake root.
"""

import os
import re
import unittest

from fixtures import ShellFixture
from helpers import REPO_ROOT

DOC = "docs/FAN_CONTROL.md"
SECTION = "10. Uninstalling just fan control"
FAN_ARTIFACTS = ("qnap-tsx70-fancontrol.service", "qnap-tsx70-fancontrol")


def read_doc():
    with open(os.path.join(REPO_ROOT, DOC), encoding="utf-8") as fh:
        return fh.read()


def section(title):
    body = read_doc()
    start = body.index("## %s" % title)
    end = body.find("\n## ", start + 1)
    return body[start:end if end > 0 else len(body)]


def bash_blocks(chunk):
    return re.findall(r"```bash\n(.*?)```", chunk, re.DOTALL)


def commands(chunk):
    """Every runnable line of every bash block, comments and blanks dropped."""
    lines = []
    for block in bash_blocks(chunk):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def documented_command():
    """The argv the guide tells the reader to run, minus sudo."""
    lines = commands(section(SECTION))
    assert lines, "no command under %r" % SECTION
    return lines[0].replace("sudo ", "", 1).split()


class DocumentedRecipeTests(unittest.TestCase):
    """What the section says, before anything is run."""

    def test_the_section_points_at_the_supported_uninstaller(self):
        argv = documented_command()
        self.assertTrue(argv[0].endswith("scripts/uninstall.sh"),
                        "the guide does not start with the uninstaller: %s"
                        % argv)
        self.assertIn("--fan-only", argv)

    def test_the_documented_command_exists(self):
        argv = documented_command()
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, argv[0])), argv)
        with open(os.path.join(REPO_ROOT, "scripts/uninstall.sh"),
                  encoding="utf-8") as fh:
            self.assertIn("--fan-only", fh.read(),
                          "the guide documents a flag the script does not take")

    def test_no_bash_block_deletes_a_fan_artifact_by_hand(self):
        # Prose may name the old recipe to warn about it. A runnable line may
        # not be it.
        for line in commands(read_doc()):
            if line.startswith(("rm ", "sudo rm ")):
                for artifact in FAN_ARTIFACTS:
                    self.assertNotIn(artifact, line,
                                     "the guide still deletes %s by hand: %s"
                                     % (artifact, line))

    def test_no_bash_block_stops_the_service_with_disable_now(self):
        for line in commands(read_doc()):
            self.assertNotIn("disable --now", line,
                             "a runnable line still uses disable --now: %s"
                             % line)


class DocumentedFanUninstallCase(unittest.TestCase):
    """Runs the guide's command against a fake root and a fake chip."""

    def setUp(self):
        self.fixture = ShellFixture(self)
        self.fixture.write(
            os.path.join(self.fixture.bin_dir, "qnap-tsx70-lcd"),
            "#!/bin/sh\nexit 0\n", 0o755)
        self.fixture.add_unit("qnap-tsx70-lcd.service", active=True,
                              enabled=True)
        self.fixture.add_unit("qnap-tsx70-fancontrol.service", active=True,
                              enabled=True)
        self.fixture.with_config()

    def with_chip(self, **chip):
        return self.fixture.with_fan_chip(**chip)

    def run_documented(self):
        return self.fixture.run_script(*documented_command())

    def assertFanArtifactsKept(self, result):
        self.assertNotEqual(result.returncode, 0,
                            "the documented command did not refuse:\n%s"
                            % result.stdout)
        self.assertTrue(
            self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"),
            "the fan binary went despite the refusal")
        self.assertTrue(
            self.fixture.exists("etc/systemd/system/qnap-tsx70-fancontrol.service"),
            "the fan unit went despite the refusal")
        self.assertTrue(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))
        self.assertNotIn("ok   removed", result.stdout,
                         "a removal was reported by a run that refused")


class DocumentedFanUninstallTests(DocumentedFanUninstallCase):
    def test_a_failed_stop_removes_nothing(self):
        self.with_chip()
        self.fixture.fail_verb("stop", "qnap-tsx70-fancontrol.service")

        result = self.run_documented()

        self.assertFanArtifactsKept(result)
        self.assertIn("could not be confirmed stopped", result.stderr)

    def test_an_ineffective_stop_removes_nothing(self):
        # `systemctl stop` exits 0 and the unit is still up. This is exactly
        # what `disable --now` in the old recipe could not see.
        self.with_chip()
        self.fixture.make_stop_ineffective("qnap-tsx70-fancontrol.service")

        result = self.run_documented()

        self.assertFanArtifactsKept(result)
        self.assertIn("still active", result.stderr)

    def test_a_deactivating_unit_removes_nothing(self):
        self.with_chip()
        self.fixture.set_active_state("qnap-tsx70-fancontrol.service",
                                      "deactivating")
        self.fixture.make_stop_ineffective("qnap-tsx70-fancontrol.service")

        result = self.run_documented()

        self.assertFanArtifactsKept(result)

    def test_a_surviving_writer_removes_nothing(self):
        # No unit is running any more, but a writer outlived it. `run --sensor
        # status` is the invocation a word-matching check called read-only.
        self.with_chip()
        self.fixture.add_process(
            8700, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3",
                  os.path.join(self.fixture.bin_dir, "qnap-tsx70-fancontrol"),
                  "run", "--sensor", "status"],
            linger=True)

        result = self.run_documented()

        self.assertFanArtifactsKept(result)
        self.assertIn("8700", result.stderr)
        self.assertTrue(self.fixture.process_exists(8700))

    def test_an_unverified_safe_state_removes_nothing(self):
        # The service is down and nothing is running, but the chip stayed in
        # manual mode. The old recipe had no way to notice and would have
        # deleted the binary that could have retried.
        self.with_chip(enable=1, ignore_writes=["pwm3_enable"])

        result = self.run_documented()

        self.assertFanArtifactsKept(result)
        self.assertIn("still in mode 1", result.stderr)
        self.assertIn("safe-state", result.stderr)
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "1")

    def test_the_happy_path_removes_the_fan_artifacts_and_nothing_else(self):
        self.with_chip(enable=1)

        result = self.run_documented()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertFalse(
            self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertFalse(
            self.fixture.exists("etc/systemd/system/qnap-tsx70-fancontrol.service"))
        self.assertTrue(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"),
                        "the documented fan-only command removed the LCD")
        self.assertTrue(
            self.fixture.exists("etc/systemd/system/qnap-tsx70-lcd.service"))
        self.assertTrue(self.fixture.exists("etc/qnap-tsx70-lcd.conf"))
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "2",
                         "the chip was never handed the fans back")


if __name__ == "__main__":
    unittest.main()
