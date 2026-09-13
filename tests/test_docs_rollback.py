"""Group 14: the documented recovery commands, executed against a fixture.

The rollback guide used to say `cp "$BACKUP"/saturn-* /usr/local/bin/`, which
against a flat backup also copies unit files and the calibration cache into
the binary directory. Nothing but a real run catches that, so this runs it.
"""

import os
import re
import subprocess
import unittest

from fixtures import ShellFixture
from helpers import REPO_ROOT

DOC = "docs/MIGRATION_FROM_SATURN.md"
LEGACY_BINARIES = ("saturn-lcd", "saturn-fan-calibrate", "saturn-fancontrol",
                   "saturn-fan")
LEGACY_UNITS = ("saturn-lcd.service", "saturn-fancontrol.service",
                "saturn-fan.service")


def read_doc():
    with open(os.path.join(REPO_ROOT, DOC), encoding="utf-8") as fh:
        return fh.read()


def code_block(section):
    """The first ```bash block under a `## <section>` heading."""
    body = read_doc()
    start = body.index("## %s" % section)
    end = body.find("\n## ", start + 1)
    chunk = body[start:end if end > 0 else len(body)]
    match = re.search(r"```bash\n(.*?)```", chunk, re.DOTALL)
    assert match, "no bash block under %r" % section
    return match.group(1)


def reroot(script, root):
    """Point every absolute system path at the fixture root."""
    return re.sub(r"(?<![\w$/.])/(etc|usr|var|run|proc)\b",
                  root + r"/\1", script)


class RollbackCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ShellFixture(self)
        self.backup = os.path.join(self.fixture.backup_root, "20260912-000000-1")
        for kind in ("bin", "systemd", "state", "config"):
            os.makedirs(os.path.join(self.backup, kind), exist_ok=True)
        for name in ("saturn-lcd", "saturn-fan-calibrate"):
            self.fixture.write(os.path.join(self.backup, "bin", name),
                               "#!/bin/sh\necho %s\n" % name, 0o755)
        for name in ("saturn-lcd.service", "saturn-fancontrol.service"):
            self.fixture.write(os.path.join(self.backup, "systemd", name),
                               "[Unit]\nDescription=%s\n" % name)
        self.fixture.write(
            os.path.join(self.backup, "state", "saturn-fan-cache.json"),
            '{"version": 1, "channels": [1]}')
        self.fixture.write(os.path.join(self.backup, "enabled-units"),
                           "saturn-lcd.service\n")
        # What the rollback is undoing.
        self.fixture.write(
            os.path.join(self.fixture.bin_dir, "qnap-tsx70-lcd"),
            "#!/bin/sh\n", 0o755)
        self.fixture.write(
            os.path.join(self.fixture.bin_dir, "qnap-tsx70-fancontrol"),
            "#!/bin/sh\n", 0o755)
        self.fixture.add_unit("qnap-tsx70-lcd.service", active=True,
                              enabled=True)
        self.fixture.add_unit("qnap-tsx70-fancontrol.service")

    def run_documented_rollback(self):
        script = code_block("4. Rolling back")
        script = reroot(script.replace("sudo ", ""), self.fixture.root)
        # The guide's placeholder becomes the fixture's real backup directory.
        script, count = re.subn(r"^BACKUP=.*$", "BACKUP=%s" % self.backup,
                                script, count=1, flags=re.MULTILINE)
        self.assertEqual(count, 1, "no BACKUP= line to point at the fixture")
        self.assertNotIn("<timestamp>", script, script)
        result = subprocess.run(["bash", "-e", "-c", script], cwd=REPO_ROOT,
                                env=self.fixture.env(), capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         "the documented rollback failed:\n%s\n%s"
                         % (script, result.stderr))
        return result

    def test_the_binary_directory_receives_only_binaries(self):
        self.run_documented_rollback()
        landed = sorted(os.listdir(self.fixture.bin_dir))
        for name in landed:
            self.assertFalse(name.endswith((".service", ".json")),
                             "%s was copied into the binary directory" % name)
        self.assertEqual(landed, ["saturn-fan-calibrate", "saturn-lcd"],
                         "unexpected contents: %s" % landed)

    def test_the_legacy_units_come_back(self):
        self.run_documented_rollback()
        units = sorted(os.listdir(self.fixture.unit_dir))
        self.assertIn("saturn-lcd.service", units)
        self.assertIn("saturn-fancontrol.service", units)
        for name in units:
            self.assertFalse(name.startswith("qnap-tsx70-"),
                             "%s survived the rollback" % name)

    def test_the_legacy_cache_comes_back(self):
        self.run_documented_rollback()
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"))

    def test_the_restored_binaries_keep_their_mode(self):
        self.run_documented_rollback()
        self.assertEqual(self.fixture.mode("usr/local/bin/saturn-lcd"), 0o755)


class ManualProcedureTests(unittest.TestCase):
    """The manual path must cover every legacy unit the installer knows."""

    def test_the_manual_migration_handles_both_fan_units_and_the_lcd(self):
        block = code_block("3. Manual migration")
        for unit in LEGACY_UNITS:
            self.assertIn(unit, block,
                          "%s is not handled by the manual procedure" % unit)

    def test_the_manual_migration_verifies_the_stop(self):
        block = code_block("3. Manual migration")
        self.assertIn("is-active", block,
                      "the manual procedure never checks that the stop worked")

    def test_the_manual_migration_names_the_binaries_explicitly(self):
        # Same defect as the rollback block: a `saturn-*` glob over a flat
        # directory does not distinguish binaries from units.
        block = code_block("3. Manual migration")
        self.assertNotIn('cp -a /etc/systemd/system/saturn-*.service \\\n'
                         '           /usr/local/bin/saturn-*', block)
        for binary in ("saturn-lcd", "saturn-fan-calibrate"):
            self.assertIn(binary, block)

    def test_the_pid_file_is_inspected_before_it_is_acted_on(self):
        block = code_block("3. Manual migration")
        self.assertIn("/proc/$pid/exe", block,
                      "the manual procedure signals a PID it never identified")


class BackupLayoutTests(unittest.TestCase):
    """What the guide describes has to be what the installer writes."""

    def test_the_documented_layout_matches_the_installer(self):
        with open(os.path.join(REPO_ROOT, "scripts/install.sh"),
                  encoding="utf-8") as fh:
            installer = fh.read()
        for kind in ("bin", "systemd", "state", "config"):
            self.assertIn('"$BACKUP_DIR/%s' % kind, installer,
                          "the installer does not write a %s/ directory" % kind)
        doc = read_doc()
        for kind in ("bin", "systemd", "state"):
            self.assertRegex(doc, r'\$BACKUP"?/%s/' % kind,
                             "the guide never restores from %s/" % kind)


if __name__ == "__main__":
    unittest.main()
