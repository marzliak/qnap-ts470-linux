"""Repository-level checks: required artifacts, naming, privacy and links.

These run in CI as a static gate. They use only the standard library, so a
network outage or a third-party action cannot make them fail spuriously.
"""

import os
import re
import subprocess
import unittest

from helpers import REPO_ROOT, load_fan, load_lcd

REQUIRED_FILES = [
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    ".gitignore",
    "bin/qnap-tsx70-lcd",
    "bin/qnap-tsx70-fancontrol",
    "config/qnap-tsx70-lcd.conf.example",
    "systemd/qnap-tsx70-lcd.service",
    "systemd/qnap-tsx70-fancontrol.service",
    "scripts/install.sh",
    "scripts/uninstall.sh",
    "scripts/diagnose.sh",
    "docs/LCD_GUIDE.md",
    "docs/LCD_PROTOCOL.md",
    "docs/FAN_CONTROL.md",
    "docs/COMPATIBILITY.md",
    "docs/MIGRATION_FROM_SATURN.md",
    ".github/workflows/ci.yml",
]

# The legacy prefix may only survive where the migration is being explained or
# performed. Anywhere else it is a leftover.
LEGACY_ALLOWED = {
    "docs/MIGRATION_FROM_SATURN.md",  # the migration document itself
    "README.md",                      # the upgrade pointer for existing users
    "CHANGELOG.md",                   # release notes for the rename
    "scripts/install.sh",             # must detect and migrate legacy artifacts
    "scripts/diagnose.sh",            # reports a half-migrated system
    "tests/test_repo_hygiene.py",     # states the rule
}

# A link to the migration document is not itself a legacy reference.
MIGRATION_DOC = "MIGRATION_FROM_SATURN.md"

SKIP_DIRS = {".git", "__pycache__", ".github"}


def tracked_files():
    """Files git knows about, so untracked scratch files cannot fail the gate."""
    try:
        out = subprocess.run(["git", "-C", REPO_ROOT, "ls-files"],
                             capture_output=True, text=True, check=True)
        files = [f for f in out.stdout.splitlines() if f]
        if files:
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for root, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            files.append(os.path.relpath(os.path.join(root, name), REPO_ROOT))
    return files


def read(path):
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def text_files():
    for path in sorted(tracked_files()):
        if path.endswith((".png", ".jpg", ".gz", ".pyc")):
            continue
        full = os.path.join(REPO_ROOT, path)
        if not os.path.isfile(full):
            continue
        yield path, read(path)


class RequiredArtifactTests(unittest.TestCase):
    def test_every_required_file_exists(self):
        for path in REQUIRED_FILES:
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, path)),
                            "missing required file: %s" % path)

    def test_executables_are_executable(self):
        for path in ("bin/qnap-tsx70-lcd", "bin/qnap-tsx70-fancontrol",
                     "scripts/install.sh", "scripts/uninstall.sh",
                     "scripts/diagnose.sh"):
            self.assertTrue(os.access(os.path.join(REPO_ROOT, path), os.X_OK),
                            "%s is not executable" % path)

    def test_no_legacy_files_remain(self):
        for path in ("install.sh", "saturn-lcd", "saturn-fan-calibrate",
                     "saturn-lcd.service"):
            self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, path)),
                             "legacy file still present: %s" % path)

    def test_handoff_files_are_absent(self):
        for path in ("CLAUDE_TASK.md", "CLAUDE_AGENTS.json"):
            self.assertNotIn(path, tracked_files(),
                             "handoff file must not be committed: %s" % path)


class NamingTests(unittest.TestCase):
    """The legacy prefix must not survive outside migration documentation."""

    def test_legacy_prefix_only_where_allowed(self):
        offenders = []
        for path, body in text_files():
            if path in LEGACY_ALLOWED:
                continue
            if re.search(r"saturn", body.replace(MIGRATION_DOC, ""), re.IGNORECASE):
                offenders.append(path)
        self.assertEqual(offenders, [],
                         "legacy prefix found outside migration docs: %s"
                         % offenders)

    def test_allowed_occurrences_are_the_legacy_prefix_form(self):
        # Where it does appear it must be the `saturn-*` namespace, never a
        # bare word that could read as a machine name.
        pattern = re.compile(r"saturn(?!-|\.md|_)", re.IGNORECASE)
        # This file states the rule, so it necessarily contains the bare word.
        for path in sorted(LEGACY_ALLOWED - {"tests/test_repo_hygiene.py"}):
            full = os.path.join(REPO_ROOT, path)
            if not os.path.isfile(full):
                continue
            for number, line in enumerate(read(path).splitlines(), 1):
                # Strip the document name first; what remains must be the
                # `saturn-*` prefix or nothing.
                stripped = line.replace(MIGRATION_DOC, "")
                self.assertIsNone(
                    pattern.search(stripped),
                    "%s:%d uses a bare legacy word: %s"
                    % (path, number, line.strip()))

    def test_binaries_use_the_new_prefix(self):
        for path in ("bin/qnap-tsx70-lcd", "bin/qnap-tsx70-fancontrol"):
            self.assertIn("qnap-tsx70", read(path))


class PrivacyTests(unittest.TestCase):
    """No host-identifying value from the reference system may be published."""

    PATTERNS = {
        "private IPv4": re.compile(
            r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))"
            r"\.\d{1,3}\.\d{1,3}\b"),
        "MAC address": re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"),
        "DMI UUID": re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        "filesystem UUID assignment": re.compile(r"\bUUID=[0-9a-fA-F-]{16,}"),
        "private key block": re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY"),
        "bearer token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})"),
    }

    # Documentation legitimately shows these as examples or placeholders.
    # RFC 5737 documentation addresses only; no real address appears anywhere.
    ALLOWED_LITERALS = ("192.0.2.1", "192.0.2.10", "0.0.0.0")

    def test_no_identifying_values_in_tracked_files(self):
        findings = []
        for path, body in text_files():
            for line_number, line in enumerate(body.splitlines(), 1):
                if any(literal in line for literal in self.ALLOWED_LITERALS):
                    continue
                for label, pattern in self.PATTERNS.items():
                    if pattern.search(line):
                        findings.append("%s:%d %s -> %s"
                                        % (path, line_number, label,
                                           line.strip()[:80]))
        self.assertEqual(findings, [], "identifying data found: %s" % findings)

    def test_no_disk_serial_numbers_published(self):
        # Memory part numbers are published product identifiers and are fine;
        # anything labelled as a serial is not.
        pattern = re.compile(r"serial\s*(?:number|no\.?|#)\s*[:=]\s*\S+", re.I)
        for path, body in text_files():
            if path in ("scripts/diagnose.sh", "tests/test_repo_hygiene.py"):
                continue
            self.assertIsNone(pattern.search(body),
                              "%s appears to publish a serial number" % path)


class MarkdownLinkTests(unittest.TestCase):
    LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

    def test_relative_links_resolve(self):
        broken = []
        for path, body in text_files():
            if not path.endswith(".md"):
                continue
            base = os.path.dirname(os.path.join(REPO_ROOT, path))
            for target in self.LINK.findall(body):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                file_part = target.split("#", 1)[0]
                if not file_part:
                    continue
                if not os.path.exists(os.path.normpath(os.path.join(base, file_part))):
                    broken.append("%s -> %s" % (path, target))
        self.assertEqual(broken, [], "broken relative links: %s" % broken)

    def test_external_links_are_https(self):
        insecure = []
        for path, body in text_files():
            if not path.endswith(".md"):
                continue
            for target in self.LINK.findall(body):
                if target.startswith("http://"):
                    insecure.append("%s -> %s" % (path, target))
        self.assertEqual(insecure, [])

    def test_anchor_links_point_at_a_real_heading(self):
        broken = []
        for path, body in text_files():
            if not path.endswith(".md"):
                continue
            slugs = set()
            for line in body.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip().lower()
                    slug = re.sub(r"[^a-z0-9 -]", "", title).replace(" ", "-")
                    slugs.add(slug)
            for target in self.LINK.findall(body):
                if not target.startswith("#"):
                    continue
                if target[1:] not in slugs:
                    broken.append("%s -> %s" % (path, target))
        self.assertEqual(broken, [], "broken anchors: %s" % broken)


class DocumentedInterfaceTests(unittest.TestCase):
    """Every flag the docs promise must actually be accepted by the parser.

    The legacy script documented `--calibrate-only`, which it never parsed.
    """

    FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")
    # A table row whose first cell is nothing but a backticked flag, which is
    # how both guides present their option tables.
    FLAG_ROW = re.compile(r"^\|\s*`(--[a-z][a-z0-9-]+)[^`]*`")

    def _documented_flags(self, program, *paths):
        """Flags this project's own docs promise for `program`.

        Only lines that name the program, or option-table rows, are scanned -
        the guides also show git, systemctl and journalctl commands, whose
        flags are none of this project's business.
        """
        # The program must be in command position: at the start of a command,
        # after sudo, or after a pipe. `journalctl -u qnap-tsx70-lcd --since`
        # passes the name as an argument, so its flags are journalctl's.
        invocation = re.compile(
            r"(?:^|[`$(]\s*|\|\s*|&&\s*|;\s*|\bsudo\s+)"
            r"(?:\./)?(?:bin/|/usr/local/bin/)?" + re.escape(program) + r"\b(.*)")
        flags = set()
        for path in paths:
            for line in read(path).splitlines():
                row = self.FLAG_ROW.match(line.strip())
                if row:
                    flags.add(row.group(1))
                    continue
                match = invocation.search(line)
                if match:
                    flags.update(self.FLAG.findall(match.group(1)))
        return flags

    def test_fan_control_flags_are_real(self):
        fan = load_fan()
        parser = fan.build_parser()
        known = {"--help", "--version"}
        for action in parser._actions:                    # noqa: SLF001
            known.update(action.option_strings)
        for sub in parser._subparsers._group_actions:     # noqa: SLF001
            for name, subparser in sub.choices.items():
                for action in subparser._actions:         # noqa: SLF001
                    known.update(action.option_strings)
        documented = self._documented_flags("qnap-tsx70-fancontrol",
                                            "docs/FAN_CONTROL.md")
        # Option-table rows in the same document also cover the installer.
        external = {"--with-fan-control", "--lcd", "--no-migrate",
                    "--skip-deps", "--purge", "--keep-modules", "--now"}
        unknown = documented - known - external
        self.assertEqual(unknown, set(),
                         "documented but unparsed fan-control flags: %s" % unknown)

    def test_lcd_flags_are_real(self):
        lcd = load_lcd()
        parser = lcd.build_parser()
        known = {"--help"}
        for action in parser._actions:                    # noqa: SLF001
            known.update(action.option_strings)
        documented = self._documented_flags("qnap-tsx70-lcd",
                                            "docs/LCD_GUIDE.md")
        # Option-table rows in the guide also cover install.sh, uninstall.sh
        # and diagnose.sh.
        external = {"--lcd", "--with-fan-control", "--dry-run", "--no-migrate",
                    "--skip-deps", "--force", "--purge", "--keep-modules",
                    "--with-smart", "--output", "--now"}
        unknown = documented - known - external
        self.assertEqual(unknown, set(),
                         "documented but unparsed LCD flags: %s" % unknown)

    def test_installer_flags_are_documented_in_its_own_help(self):
        body = read("scripts/install.sh")
        for flag in ("--lcd", "--with-fan-control", "--dry-run", "--no-migrate",
                     "--skip-deps", "--force"):
            self.assertIn(flag, body)


if __name__ == "__main__":
    unittest.main()
