"""Group 14: the documented recovery commands, executed against a fixture.

The rollback guide used to say `cp "$BACKUP"/saturn-* /usr/local/bin/`, which
against a flat backup also copies unit files and the calibration cache into
the binary directory. Nothing but a real run catches that, so this runs it.
"""

import os
import re
import subprocess
import unittest

from fixtures import VALID_CACHE, ShellFixture
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


def point_at_the_fixture_fan_binary(script, fixture, case):
    """Rewrite the guide's `FANCTL=` line to the fixture's fan program.

    Same treatment the rollback block's `BACKUP=` placeholder already gets. The
    guide names the repository copy, which is right for a reader and unusable
    here: it would need root and a real f71882fg. The fixture's program is
    that same repository source with geteuid faked and a disposable chip
    behind it, so what runs is the shipped code either way.
    """
    script, count = re.subn(r'^FANCTL=.*$',
                            'FANCTL="python3 %s"' % fixture.fan_binary,
                            script, count=1, flags=re.MULTILINE)
    case.assertEqual(count, 1,
                     "the documented block no longer defines FANCTL, so it "
                     "has no way to ask whether the fans came back")
    return script


def reroot(script, root, proc_dir=None):
    """Point every absolute system path at the fixture root.

    /proc gets its own mapping: the fixture's fake process table lives outside
    the fake root so it is not part of any snapshot, and the guard blocks in
    the guide walk /proc directly.
    """
    script = re.sub(r"(?<![\w$/.])/proc\b",
                    (proc_dir or (root + "/proc")).replace("\\", "\\\\"),
                    script)
    return re.sub(r"(?<![\w$/.])/(etc|usr|var|run|dev)\b",
                  root + r"/\1", script)


def manual_preamble():
    """Step 0 of the manual procedure: the guards every later step calls.

    The numbered steps are not independent, so a test that runs one of them
    has to bring the guards with it - which is also the point: a step that
    could run without them would be a step that deletes without checking.
    """
    block = code_block("3. Manual migration")
    preamble = block[:block.index("# 1.")]
    assert "abort()" in preamble, "step 0 no longer defines the guards"
    assert "require_quiescent" in preamble, preamble
    return preamble


def manual_script(marker=None):
    """The manual block, optionally starting at `marker` with step 0 prepended."""
    block = code_block("3. Manual migration")
    if marker is None:
        return block
    return manual_preamble() + block[block.index(marker):]


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
        script = reroot(script.replace("sudo ", ""), self.fixture.root,
                        self.fixture.proc_dir)
        script = point_at_the_fixture_fan_binary(script, self.fixture, self)
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


class ManualExecutionCase(unittest.TestCase):
    """Runs the guide's manual block, or part of it, against a fake root."""

    def setUp(self):
        self.fixture = ShellFixture(self)
        self.fixture.with_config()

    def with_legacy_tree(self, cache=VALID_CACHE, fan_active=False,
                         lcd_active=False):
        fixture = self.fixture
        for name in LEGACY_BINARIES:
            fixture.write(os.path.join(fixture.bin_dir, name),
                          "#!/bin/sh\necho %s\n" % name, 0o755)
        for name in LEGACY_UNITS:
            fixture.add_unit(name, active=False, enabled=True)
        fixture.add_unit("saturn-fancontrol.service", active=fan_active,
                         enabled=True)
        fixture.add_unit("saturn-lcd.service", active=lcd_active, enabled=True)
        if cache is not None:
            fixture.write(fixture.legacy_cache, cache)
        return fixture

    def run_manual(self, marker=None, expect=0):
        script = reroot(manual_script(marker).replace("sudo ", ""),
                        self.fixture.root, self.fixture.proc_dir)
        script = point_at_the_fixture_fan_binary(script, self.fixture, self)
        result = subprocess.run(["bash", "-e", "-c", script], cwd=REPO_ROOT,
                                env=self.fixture.env(), capture_output=True,
                                text=True, timeout=180)
        if expect == 0:
            self.assertEqual(result.returncode, 0,
                             "the documented procedure failed:\n%s\n%s"
                             % (script, result.stderr))
        else:
            self.assertNotEqual(result.returncode, 0,
                                "the documented procedure did not refuse:\n%s"
                                % result.stdout)
        return result

    def assertLegacyTreeIntact(self, result):
        self.assertIn("ABORT", result.stderr, result.stderr)
        for name in LEGACY_BINARIES:
            self.assertTrue(self.fixture.exists("usr/local/bin/%s" % name),
                            "%s was removed by a procedure that aborted" % name)
        for name in LEGACY_UNITS:
            self.assertTrue(
                self.fixture.exists("etc/systemd/system/%s" % name),
                "%s was removed by a procedure that aborted" % name)
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"),
                        "the calibration cache went with the abort")
        self.assertEqual(self.fixture.kill_calls(), [],
                         "the manual procedure signalled something")

    def assertRefusedBeforeInstalling(self, result):
        """Nothing removed is the floor; nothing changed is the guarantee.

        Each numbered step has its own gate, so the block has to refuse at the
        first one that fails rather than carry on and be caught later by the
        gate in front of the deletions. Otherwise the new service is installed
        and started on a machine that was never quiescent.
        """
        self.assertLegacyTreeIntact(result)
        self.assertFalse(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"),
                         "the block installed the new service before refusing")
        self.assertNotIn("start qnap-tsx70-lcd.service", self.fixture.calls(),
                         "the block started the new service before refusing")


class ManualQuiescenceTests(ManualExecutionCase):
    """No artifact may go while a fan writer might still be alive.

    Step 1 used to read `systemctl disable --now "$unit" 2>/dev/null || true`
    and step 2 merely printed `is-active` for each unit. Step 8 then deleted
    the units and the binaries regardless, so a reader who pasted the block on
    a machine whose fan service would not stop destroyed exactly what the
    automatic path refuses to touch.
    """

    def test_a_failed_fan_stop_removes_nothing(self):
        self.with_legacy_tree(fan_active=True)
        self.fixture.fail_verb("stop", "saturn-fancontrol.service")

        result = self.run_manual(expect=1)

        self.assertLegacyTreeIntact(result)
        self.assertIn("stop saturn-fancontrol.service failed", result.stderr)

    def test_a_stop_that_leaves_the_unit_active_removes_nothing(self):
        # `systemctl stop` returned 0; the unit is still up. Only a check
        # after the fact catches this, which is what step 2 is for.
        self.with_legacy_tree(fan_active=True)
        self.fixture.make_stop_ineffective("saturn-fancontrol.service")

        result = self.run_manual(expect=1)

        self.assertRefusedBeforeInstalling(result)
        self.assertIn("still active", result.stderr)

    def test_an_activating_fan_unit_removes_nothing(self):
        # `systemctl is-active` exits nonzero for a unit on its way up, so a
        # guard written around the exit status would call this one stopped.
        self.with_legacy_tree()
        self.fixture.set_active_state("saturn-fancontrol.service", "activating")
        self.fixture.make_stop_ineffective("saturn-fancontrol.service")

        result = self.run_manual(expect=1)

        self.assertRefusedBeforeInstalling(result)

    def test_a_legacy_writer_that_outlived_its_unit_removes_nothing(self):
        self.with_legacy_tree()
        self.fixture.add_process(
            7801, "saturn-fancontrol",
            exe=os.path.join(self.fixture.bin_dir, "saturn-fancontrol"),
            argv=[os.path.join(self.fixture.bin_dir, "saturn-fancontrol")])

        result = self.run_manual(expect=1)

        self.assertRefusedBeforeInstalling(result)
        self.assertIn("7801", result.stderr)
        self.assertTrue(self.fixture.process_exists(7801))

    def test_a_new_fan_writer_removes_nothing_either(self):
        # The migration deletes the legacy fan binaries, and a `qnap-tsx70-*`
        # writer is just as able to be holding the registers while it happens.
        # `run --sensor status` is the invocation a word-matching guard called
        # read-only.
        self.with_legacy_tree()
        self.fixture.add_process(
            7802, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3",
                  os.path.join(self.fixture.bin_dir, "qnap-tsx70-fancontrol"),
                  "run", "--sensor", "status"])

        result = self.run_manual(expect=1)

        self.assertRefusedBeforeInstalling(result)
        self.assertIn("7802", result.stderr)

    def test_a_pid_file_naming_a_live_stranger_removes_nothing(self):
        # PIDs are reused. The file says 7803; 7803 is sshd. Nothing may be
        # signalled, and nothing may be deleted on that evidence either.
        self.with_legacy_tree()
        self.fixture.write(self.fixture.legacy_pid, "7803\n")
        self.fixture.add_process(7803, "sshd", exe="/usr/sbin/sshd",
                                 argv=["sshd", "-D"])

        result = self.run_manual(expect=1)

        self.assertRefusedBeforeInstalling(result)
        self.assertIn("7803", result.stderr)
        self.assertTrue(self.fixture.exists("run/saturn-fancontrol.pid"))
        self.assertTrue(self.fixture.process_exists(7803))

    def test_a_held_serial_port_removes_nothing(self):
        self.with_legacy_tree()
        self.fixture.add_process(7804, "socat", exe="/usr/bin/socat",
                                 argv=["socat", "-", "/dev/ttyS1"])
        self.fixture.hold_port(7804)

        result = self.run_manual(expect=1)

        self.assertRefusedBeforeInstalling(result)
        self.assertIn("still held", result.stderr)

    def test_the_gate_runs_again_before_the_deletions(self):
        """Step 8 checks for itself rather than trusting step 2.

        The new LCD service is started in step 7, and anything could have come
        up with it, so the step that deletes has to ask again.
        """
        script = manual_script("# 8. Only once that works")
        self.assertIn("require_quiescent", script.split("# 8.", 1)[1],
                      "step 8 deletes without re-checking")

        self.with_legacy_tree()
        self.fixture.add_process(
            7805, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3",
                  os.path.join(self.fixture.bin_dir, "saturn-fancontrol"),
                  "run"])

        result = self.run_manual("# 8. Only once that works", expect=1)

        self.assertLegacyTreeIntact(result)


class ManualHandbackTests(ManualExecutionCase):
    """Step 8 deletes the legacy fan binaries, so it proves the handback first.

    The legacy daemon is the one most likely to have left a fan in manual
    mode - it is what the reader is migrating away from - and once its binary
    is gone there is nothing on the machine that knows how to undo that until
    the new one is installed and calibrated.
    """

    def test_a_verified_handback_lets_the_migration_finish(self):
        # The control: the chip takes the write, so the removals happen.
        self.with_legacy_tree()
        self.fixture.with_fan_controller(enable=1)

        result = self.run_manual()

        self.assertIn("manual migration complete", result.stdout)
        self.assertIn("safe-state", " ".join(self.fixture.fan_commands()))
        self.assertFalse(self.fixture.exists("usr/local/bin/saturn-fancontrol"))
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "2")

    def test_a_chip_that_stays_in_manual_mode_removes_nothing(self):
        self.with_legacy_tree()
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.run_manual(expect=1)

        self.assertLegacyTreeIntact(result)
        self.assertIn("could not be confirmed back", result.stderr)

    def test_a_controller_that_is_not_there_removes_nothing(self):
        self.with_legacy_tree()
        self.fixture.without_fan_controller()

        result = self.run_manual(expect=1)

        self.assertLegacyTreeIntact(result)

    def test_a_refused_register_write_removes_nothing(self):
        self.with_legacy_tree()
        self.fixture.with_fan_controller(enable=1, fail_writes=["pwm3_enable"])

        result = self.run_manual(expect=1)

        self.assertLegacyTreeIntact(result)

    def test_an_unreadable_mode_register_removes_nothing(self):
        self.with_legacy_tree()
        self.fixture.with_fan_controller(enable="",
                                         ignore_writes=["pwm3_enable"])

        result = self.run_manual(expect=1)

        self.assertLegacyTreeIntact(result)

    def test_a_chip_with_no_controllable_channel_removes_nothing(self):
        self.with_legacy_tree()
        self.fixture.with_fan_controller(controllable=False)

        result = self.run_manual(expect=1)

        self.assertLegacyTreeIntact(result)

    def test_the_handback_is_only_asked_for_once_nothing_is_writing(self):
        self.with_legacy_tree(fan_active=True)
        self.fixture.make_stop_ineffective("saturn-fancontrol.service")

        result = self.run_manual(expect=1)

        self.assertRefusedBeforeInstalling(result)
        self.assertEqual(self.fixture.fan_commands(), [],
                         "the chip was written to while a writer was alive")

    def test_the_gate_sits_in_front_of_the_first_fan_removal(self):
        """Read as well as run: position in the block is the guarantee.

        Every failure test above would also pass if the check were made after
        the deletions and simply reported. It has to be before them.
        """
        block = code_block("3. Manual migration")
        step8 = block.split("# 8. Only once that works", 1)[1]
        self.assertIn("require_fan_handback", step8)
        self.assertLess(step8.index("require_fan_handback"),
                        step8.index("rm -f"),
                        "the handback is checked after the first removal")


class ManualHappyPathTests(ManualExecutionCase):
    """The whole block, start to finish, on a machine that cooperates."""

    def test_the_legacy_tree_goes_and_the_new_service_comes_up(self):
        self.with_legacy_tree(fan_active=True, lcd_active=True)
        self.fixture.write(self.fixture.legacy_pid, "7900\n")   # stale

        result = self.run_manual()

        self.assertIn("manual migration complete", result.stdout)
        for name in LEGACY_BINARIES:
            self.assertFalse(self.fixture.exists("usr/local/bin/%s" % name),
                             "%s survived the documented migration" % name)
        for name in LEGACY_UNITS:
            self.assertFalse(
                self.fixture.exists("etc/systemd/system/%s" % name))
        self.assertFalse(self.fixture.exists("run/saturn-fancontrol.pid"))
        self.assertTrue(self.fixture.exists("usr/local/bin/qnap-tsx70-lcd"))
        self.assertEqual(self.fixture.mode("usr/local/bin/qnap-tsx70-lcd"),
                         0o755)
        self.assertTrue(
            self.fixture.unit_state("qnap-tsx70-lcd.service").get("active"))
        self.assertEqual(self.fixture.kill_calls(), [],
                         "the manual procedure signalled something")

    def test_an_existing_configuration_is_not_overwritten(self):
        # The example config is a starting point, not an upgrade. Step 7 used
        # to install it over whatever was there.
        mine = "serial_port = %s\nrotate_seconds = 9\n" % self.fixture.port
        self.fixture.write(self.fixture.conf_file, mine)
        self.with_legacy_tree()

        self.run_manual()

        self.assertEqual(self.fixture.read("etc/qnap-tsx70-lcd.conf"), mine,
                         "the documented procedure overwrote the config")

    def test_the_backup_is_written_before_anything_is_deleted(self):
        self.with_legacy_tree()

        self.run_manual()

        for name in LEGACY_BINARIES:
            self.assertTrue(
                self.fixture.exists("var/backups/qnap-tsx70/manual/bin/%s"
                                    % name),
                "%s was deleted without a backup" % name)
        for name in LEGACY_UNITS:
            self.assertTrue(
                self.fixture.exists("var/backups/qnap-tsx70/manual/systemd/%s"
                                    % name))
        self.assertTrue(self.fixture.exists(
            "var/backups/qnap-tsx70/manual/state/saturn-fan-cache.json"))


class ManualCacheProvenanceTests(ManualExecutionCase):
    """Step 9 deletes the legacy cache only on step 6's own evidence.

    The condition used to be "does a valid calibration exist at the new path",
    which is true whenever one was already installed - so an invalid legacy
    cache that step 6 deliberately refused to migrate was deleted anyway, and
    the copy in the backup became the only one left.
    """

    INVALID = '{"channels": [1], '     # truncated on purpose

    def installed_cache(self, content):
        return self.fixture.write(
            os.path.join(self.fixture.state_dir, "fan-calibration.json"),
            content)

    def test_a_valid_legacy_cache_is_migrated_and_then_removed(self):
        self.with_legacy_tree(cache=VALID_CACHE)

        result = self.run_manual()

        self.assertIn("migrated /etc/saturn-fan-cache.json",
                      result.stdout.replace(self.fixture.root, ""))
        self.assertFalse(self.fixture.exists("etc/saturn-fan-cache.json"),
                         "a migrated cache was left behind")
        self.assertEqual(
            self.fixture.read("var/lib/qnap-tsx70/fan-calibration.json"),
            VALID_CACHE)

    def test_an_invalid_legacy_cache_is_kept(self):
        self.with_legacy_tree(cache=self.INVALID)

        result = self.run_manual()

        self.assertIn("does not validate", result.stdout)
        self.assertIn("keeping", result.stdout)
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"),
                        "the guide deleted a cache that never migrated")
        self.assertFalse(
            self.fixture.exists("var/lib/qnap-tsx70/fan-calibration.json"),
            "an invalid cache was installed anyway")

    def test_an_invalid_legacy_cache_survives_a_valid_installed_one(self):
        """The matrix case the old condition got wrong.

        Nothing migrated: the legacy file is invalid and the installed
        calibration is somebody else's. Deleting the legacy file here destroys
        the only copy of it outside the backup.
        """
        self.with_legacy_tree(cache=self.INVALID)
        self.installed_cache(VALID_CACHE)

        result = self.run_manual()

        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"),
                        "a legacy cache was deleted because an unrelated "
                        "calibration happened to validate")
        self.assertIn("step 6 did not migrate it", result.stdout)
        self.assertEqual(
            self.fixture.read("var/lib/qnap-tsx70/fan-calibration.json"),
            VALID_CACHE, "the installed calibration was overwritten")

    def test_a_valid_legacy_cache_survives_a_valid_installed_one(self):
        # Superseded, not migrated: the newer calibration wins and the legacy
        # file stays, exactly as the automatic path reports it.
        mine = '{"version": 1, "channels": [1, 2], "fans": {}}'
        self.with_legacy_tree(cache=VALID_CACHE)
        self.installed_cache(mine)

        result = self.run_manual()

        self.assertIn("not overwriting it", result.stdout)
        self.assertTrue(self.fixture.exists("etc/saturn-fan-cache.json"))
        self.assertEqual(
            self.fixture.read("var/lib/qnap-tsx70/fan-calibration.json"), mine)

    def test_no_legacy_cache_at_all_is_not_an_error(self):
        self.with_legacy_tree(cache=None)

        result = self.run_manual()

        self.assertIn("no legacy calibration cache to migrate", result.stdout)


class RollbackExecutionCase(unittest.TestCase):
    """Runs the guide's rollback block against a fake root."""

    def setUp(self):
        self.fixture = ShellFixture(self)
        for name in ("qnap-tsx70-lcd", "qnap-tsx70-fancontrol"):
            self.fixture.write(os.path.join(self.fixture.bin_dir, name),
                               "#!/bin/sh\n", 0o755)
        self.fixture.add_unit("qnap-tsx70-lcd.service", active=True,
                              enabled=True)
        self.fixture.add_unit("qnap-tsx70-fancontrol.service", active=True,
                              enabled=True)
        self.backup = os.path.join(self.fixture.backup_root, "manual")
        os.makedirs(os.path.join(self.backup, "systemd"), exist_ok=True)
        os.makedirs(os.path.join(self.backup, "bin"), exist_ok=True)
        os.makedirs(os.path.join(self.backup, "state"), exist_ok=True)
        self.fixture.write(os.path.join(self.backup, "enabled-units"),
                           "saturn-lcd.service\n")

    def run_rollback(self):
        script = code_block("4. Rolling back")
        script = reroot(script.replace("sudo ", ""), self.fixture.root,
                        self.fixture.proc_dir)
        script = point_at_the_fixture_fan_binary(script, self.fixture, self)
        script = re.sub(r"^BACKUP=.*$", "BACKUP=%s" % self.backup, script,
                        count=1, flags=re.MULTILINE)
        return subprocess.run(["bash", "-e", "-c", script], cwd=REPO_ROOT,
                              env=self.fixture.env(), capture_output=True,
                              text=True, timeout=120)

    def assertNewTreeIntact(self, result):
        self.assertNotEqual(result.returncode, 0,
                            "the rollback did not refuse:\n%s" % result.stdout)
        self.assertIn("ABORT", result.stderr, result.stderr)
        for path in ("usr/local/bin/qnap-tsx70-fancontrol",
                     "usr/local/bin/qnap-tsx70-lcd",
                     "etc/systemd/system/qnap-tsx70-fancontrol.service"):
            self.assertTrue(self.fixture.exists(path),
                            "%s was removed despite the refusal" % path)


class RollbackQuiescenceTests(RollbackExecutionCase):
    """The rollback block deletes the new binaries, so it guards them too.

    It used to run `systemctl disable --now qnap-tsx70-lcd
    qnap-tsx70-fancontrol 2>/dev/null || true` and then `rm -f` the fan
    binary - the one whose shutdown handler hands the fans back to the chip.
    """

    def test_a_failed_fan_stop_removes_nothing(self):
        # The exit status of the stop is its own guarantee. A `|| true` here
        # was swallowing a unit whose ExecStop failed - the handler that hands
        # the fans back - even where systemd went on to report it inactive,
        # so the later is-active check would have waved it through.
        self.fixture.fail_verb("stop", "qnap-tsx70-fancontrol.service")

        result = self.run_rollback()

        self.assertNewTreeIntact(result)
        self.assertIn("systemctl stop qnap-tsx70-fancontrol.service failed",
                      result.stderr)

    def test_a_stop_that_leaves_the_unit_active_removes_nothing(self):
        self.fixture.make_stop_ineffective("qnap-tsx70-fancontrol.service")

        result = self.run_rollback()

        self.assertNewTreeIntact(result)
        self.assertIn("still active", result.stderr)

    def test_a_surviving_fan_writer_removes_nothing(self):
        self.fixture.add_process(
            7950, "python3", exe=self.fixture.python_stub_versioned,
            argv=["python3",
                  os.path.join(self.fixture.bin_dir, "qnap-tsx70-fancontrol"),
                  "run", "--sensor", "status"])

        result = self.run_rollback()

        self.assertNewTreeIntact(result)
        self.assertIn("7950", result.stderr)
        self.assertTrue(self.fixture.process_exists(7950))


class RollbackHandbackTests(RollbackExecutionCase):
    """The rollback deletes the fan binary, so it proves the handback first.

    Quiescence is not the same question. A unit that stopped and a process
    table with nothing in it are both equally true of a machine whose fan is
    parked at a calibration stall duty in manual mode - which is exactly the
    machine somebody rolls back from. Deleting the binary there removes the
    only thing that could have fixed it.
    """

    def test_a_verified_handback_lets_the_rollback_proceed(self):
        # The control. The chip is in manual mode and takes the write, so the
        # deletions happen - and the chip ends up in automatic mode.
        self.fixture.with_fan_controller(enable=1)

        result = self.run_rollback()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("safe-state", " ".join(self.fixture.fan_commands()))
        self.assertFalse(
            self.fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))
        self.assertEqual(self.fixture.fan_register("pwm3_enable"), "2")

    def test_a_chip_that_stays_in_manual_mode_removes_nothing(self):
        self.fixture.with_fan_controller(enable=1,
                                         ignore_writes=["pwm3_enable"])

        result = self.run_rollback()

        self.assertNewTreeIntact(result)
        self.assertIn("could not be confirmed back", result.stderr)

    def test_a_controller_that_is_not_there_removes_nothing(self):
        # The driver is not loaded, so there is no chip to ask. That is not
        # permission to delete the program that would have asked it.
        self.fixture.without_fan_controller()

        result = self.run_rollback()

        self.assertNewTreeIntact(result)

    def test_a_refused_register_write_removes_nothing(self):
        self.fixture.with_fan_controller(enable=1, fail_writes=["pwm3_enable"])

        result = self.run_rollback()

        self.assertNewTreeIntact(result)

    def test_an_unreadable_mode_register_removes_nothing(self):
        self.fixture.with_fan_controller(enable="",
                                         ignore_writes=["pwm3_enable"])

        result = self.run_rollback()

        self.assertNewTreeIntact(result)

    def test_a_chip_with_no_controllable_channel_removes_nothing(self):
        # A tachometer and no duty registers: nothing can hand these fans
        # back, so nothing may claim it did.
        self.fixture.with_fan_controller(controllable=False)

        result = self.run_rollback()

        self.assertNewTreeIntact(result)

    def test_the_handback_is_only_asked_for_once_nothing_is_writing(self):
        # Order matters: asking the chip to take the fans back while a writer
        # is still driving them is answered by whichever of the two wrote
        # last. The block must refuse before it ever runs safe-state.
        self.fixture.make_stop_ineffective("qnap-tsx70-fancontrol.service")

        result = self.run_rollback()

        self.assertNewTreeIntact(result)
        self.assertEqual(self.fixture.fan_commands(), [],
                         "the chip was written to while a writer was alive")
