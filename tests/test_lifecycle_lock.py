"""The lifecycle scripts hold the lock the fan writers take.

`qnap-tsx70-fancontrol run` and `calibrate` serialise on a global lock under
/run before their first register write. install.sh, uninstall.sh and rollback()
replace and delete the binary those writers execute - and until now they proved
the coast was clear by reading the process table once and then acting on that
reading for the rest of the run.

A point-in-time check cannot cover the window after it. Between "no
fan-control process remains" and the `install` or the `rm` there is a verified
handback, a display clear and a daemon-reload, and any of a boot-time
activation, a `systemctl start`, or an operator's `calibrate` landing in that
gap is precisely the thing the check was protecting against: a live writer
whose executable is being replaced or removed underneath it, with the fan
possibly parked at a stall duty.

So the three of them take the same lock and hold it across the recheck, the
handback and every replacement or removal. These tests prove that by starting
a *real* writer - the shipped program, in another process - at a moment that
is inside the window by construction rather than by timing: the verifier's
`safe-state` invocation, which each lifecycle reaches after taking the lock
and before touching a file.

The writer names a channel the fake chip does not have, so a run that gets
past the lock stops immediately with "no controllable fan channel" and sweeps
nothing. That message is the proof of ownership; "another qnap-tsx70-fancontrol
instance holds" is the proof of exclusion.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import VALID_CACHE, ShellFixture
from helpers import REPO_ROOT

FAN_UNIT = "qnap-tsx70-fancontrol.service"
LCD_UNIT = "qnap-tsx70-lcd.service"

REFUSED = "another qnap-tsx70-fancontrol instance holds"
ACQUIRED = "no controllable fan channel is spinning"


class LockCase(unittest.TestCase):
    def setUp(self):
        self.fixture = ShellFixture(self)
        self.fixture.with_config()

    def assertRefusedAtTheLock(self, attempts):
        self.assertTrue(attempts,
                        "no writer was started inside the locked window")
        for code, output in attempts:
            self.assertNotEqual(code, 0, output)
            self.assertIn(REFUSED, output,
                          "a real writer started while the lifecycle script "
                          "was supposed to own the lock:\n%s" % output)
            self.assertNotIn(ACQUIRED, output,
                             "the writer got past the lock:\n%s" % output)

    def assertAcquires(self, result):
        self.assertIn(ACQUIRED, result.stdout,
                      "a writer could not take a lock nobody should hold:\n%s"
                      % result.stdout)
        self.assertNotIn(REFUSED, result.stdout, result.stdout)

    def lock_path(self):
        return os.path.join(self.fixture.lock_dir, "fancontrol.lock")


# What a fan writer itself resolves, asked of the program directly. Kept
# separate from the helper on purpose: a parity test in which both sides are
# the same code proves nothing.
WRITER_SIDE = (
    "import importlib.machinery, importlib.util, os, sys\n"
    "loader = importlib.machinery.SourceFileLoader('f', sys.argv[1])\n"
    "spec = importlib.util.spec_from_loader('f', loader)\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "loader.exec_module(m)\n"
    "print(m.writer_lock_paths(device='/sys/devices/platform/f71882fg.2592')[0])\n")


class LockPathParityTests(unittest.TestCase):
    """One definition of "the canonical lock", read by both sides.

    scripts/qnap_lock.py does not define a path; it reads the one the writers
    take. If that stopped being true the lifecycle scripts would hold a lock
    nothing contends for, and the race tests below would keep passing against
    it - the writer would be refused by a lock it never looked at only because
    it was refused by the one it did. So the two entry points are asked
    independently, in separate processes, and compared.
    """

    def ask(self, argv, root):
        env = dict(os.environ)
        env["QNAP_TSX70_LOCK_DIR"] = root
        result = subprocess.run(argv, env=env, cwd=REPO_ROOT,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout.strip()

    def test_the_helper_and_the_fan_program_agree(self):
        for root in ("/run/qnap-tsx70", "/tmp/qnap-tsx70-parity-check"):
            with self.subTest(root=root):
                helper = self.ask(
                    [sys.executable,
                     os.path.join(REPO_ROOT, "scripts/qnap_lock.py"), "path"],
                    root)
                writer = self.ask(
                    [sys.executable, "-c", WRITER_SIDE,
                     os.path.join(REPO_ROOT, "bin/qnap-tsx70-fancontrol")],
                    root)
                self.assertEqual(helper, writer)
                self.assertEqual(helper, os.path.join(root, "fancontrol.lock"))

    def test_the_global_lock_is_the_one_a_writer_takes_first(self):
        # Holding it is enough to exclude every writer, whatever controller it
        # resolved, which is why the shell needs no device discovery.
        from helpers import load_fan
        fan = load_fan()
        paths = fan.writer_lock_paths(device="/sys/devices/platform/f71882fg.1")
        self.assertEqual(paths[0],
                         os.path.join(fan.LOCK_ROOT, fan.GLOBAL_LOCK_NAME))
        self.assertGreater(len(paths), 1, "the per-controller lock is gone")

    def test_no_command_line_input_can_move_the_lock(self):
        # The only thing that may move it is QNAP_TSX70_LOCK_DIR, which is the
        # fan program's own seam. Nothing a caller passes may - which is what
        # keeps a `--cache` from selecting one.
        for extra in (["--cache", "/tmp/elsewhere"],
                      ["--device-glob", "/tmp/elsewhere"],
                      ["--fan-binary", "/tmp/elsewhere"]):
            with self.subTest(extra=extra):
                result = subprocess.run(
                    [sys.executable,
                     os.path.join(REPO_ROOT, "scripts/qnap_lock.py"), "path"]
                    + extra, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True, timeout=60)
                self.assertNotEqual(result.returncode, 0,
                                    "the helper accepted %s" % extra)

    def test_neither_lifecycle_script_offers_an_override(self):
        # A seam naming the helper, or the lock path, would be a way to point
        # a run at a lock no writer takes.
        for script in ("scripts/install.sh", "scripts/uninstall.sh"):
            with open(os.path.join(REPO_ROOT, script), encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn('FAN_LOCK_HELPER="$REPO_ROOT/scripts/qnap_lock.py"',
                          body, "%s no longer resolves the helper directly"
                          % script)
            self.assertNotIn("QNAP_TSX70_LOCK_HELPER", body, script)
            self.assertNotIn("QNAP_TSX70_LOCK_PATH", body, script)


class InstallLockTests(LockCase):
    """A --with-fan-control install over an existing fan installation."""

    def existing_fan_install(self, active=True):
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\necho previous\n", 0o755)
        fixture.add_unit(FAN_UNIT, active=active, enabled=True,
                         content="[Unit]\nDescription=previous\n")
        fixture.write(os.path.join(fixture.state_dir,
                                   "fan-calibration.json"), VALID_CACHE)
        fixture.with_fan_controller(enable=1)
        return fixture

    def test_a_writer_cannot_start_before_the_binary_is_replaced(self):
        fixture = self.existing_fan_install()
        fixture.with_race_writer()

        result = fixture.install("--skip-deps", "--with-fan-control")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertRefusedAtTheLock(fixture.race_attempts())
        # And the replacement really did happen after that attempt.
        with open(os.path.join(REPO_ROOT, "bin/qnap-tsx70-fancontrol"),
                  encoding="utf-8") as fh:
            self.assertEqual(fixture.read("usr/local/bin/qnap-tsx70-fancontrol"),
                             fh.read())

    def test_the_lock_is_reported_and_released(self):
        fixture = self.existing_fan_install()

        result = fixture.install("--skip-deps", "--with-fan-control")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("holding the fan writer lock %s" % self.lock_path(),
                      result.stdout)
        self.assertIn("released the fan writer lock", result.stdout)

    def test_the_lock_is_acquirable_once_the_install_is_over(self):
        fixture = self.existing_fan_install()
        fixture.install("--skip-deps", "--with-fan-control")

        self.assertAcquires(fixture.run_writer())

    def test_a_migration_holds_it_too(self):
        # The legacy tree is in scope, so its fan artifacts are the ones being
        # deleted - and the new fan binary is being written at the same time.
        fixture = self.fixture
        fixture.with_legacy()
        fixture.with_fan_controller(enable=1)
        fixture.with_race_writer()

        result = fixture.install("--skip-deps", "--with-fan-control")

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertRefusedAtTheLock(fixture.race_attempts())
        self.assertFalse(fixture.exists("usr/local/bin/saturn-fancontrol"))

    def test_an_lcd_only_install_never_takes_it(self):
        # No fan artifact in scope: nothing of fan control is replaced or
        # removed, so there is no writer to exclude and taking the lock would
        # only block one for no reason.
        fixture = self.existing_fan_install()

        result = fixture.install("--skip-deps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("holding the fan writer lock", result.stdout)
        self.assertFalse(os.path.exists(self.lock_path()),
                         "an LCD-only install created the fan writer lock")

    def test_a_writer_that_already_holds_it_stops_the_install(self):
        # The other direction: the lock is not advisory decoration. A real
        # writer owning it means the installer refuses rather than replacing
        # the binary underneath it.
        fixture = self.existing_fan_install(active=False)
        holder = self.hold_the_lock()

        result = fixture.install("--skip-deps", "--with-fan-control")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("another fan writer holds", result.stderr)
        self.assertEqual(fixture.read("usr/local/bin/qnap-tsx70-fancontrol"),
                         "#!/bin/sh\necho previous\n",
                         "the fan binary was replaced under a live writer")
        self.assertIsNone(holder.poll(), "the holder died before the assertion")

    def hold_the_lock(self):
        """A process holding the canonical lock, released on cleanup."""
        script = (
            "import fcntl, os, sys, time\n"
            "os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)\n"
            "h = open(sys.argv[1], 'w')\n"
            "fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
            "time.sleep(600)\n")
        holder = subprocess.Popen([sys.executable, "-c", script,
                                   self.lock_path()],
                                  stdout=subprocess.PIPE,
                                  universal_newlines=True)
        self.addCleanup(self._reap, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()


class UninstallLockTests(LockCase):
    """Removal is the sharpest window: the binary goes and the unit with it."""

    def installed(self, **chip):
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit(LCD_UNIT, active=True, enabled=True)
        fixture.add_unit(FAN_UNIT, active=True, enabled=True)
        fixture.with_fan_chip(enable=1, **chip)
        return fixture

    def test_a_writer_cannot_start_before_the_binary_is_deleted(self):
        fixture = self.installed()
        fixture.with_race_writer()

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertRefusedAtTheLock(fixture.race_attempts())
        self.assertFalse(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"))

    def test_the_lock_is_taken_before_the_quiescence_recheck(self):
        fixture = self.installed()

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        # Asserted present before it is located: a run that never took the
        # lock has to fail on this line rather than on a ValueError from the
        # ordering check below it.
        self.assertIn("holding the fan writer lock", result.stdout,
                      "the uninstaller never took the fan writer lock")
        self.assertIn("no fan-control process remains", result.stdout)
        self.assertIn("Handing the fans back", result.stdout)
        self.assertLess(
            result.stdout.index("holding the fan writer lock"),
            result.stdout.index("no fan-control process remains"),
            "the recheck ran before the lock that makes it mean anything")
        self.assertLess(result.stdout.index("holding the fan writer lock"),
                        result.stdout.index("Handing the fans back"))

    def test_the_lock_is_acquirable_once_the_uninstall_is_over(self):
        fixture = self.installed()
        fixture.uninstall()

        self.assertAcquires(fixture.run_writer())

    def test_an_uninstall_with_no_fan_control_never_takes_it(self):
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\nexit 0\n", 0o755)
        fixture.add_unit(LCD_UNIT, active=True, enabled=True)

        result = fixture.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("holding the fan writer lock", result.stdout)
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_a_dry_run_previews_the_hold_and_takes_nothing(self):
        fixture = self.installed()
        before = fixture.snapshot()

        result = fixture.uninstall("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertIn("[dry-run] hold the fan writer lock", result.stdout)
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_a_refused_uninstall_still_leaves_the_lock_acquirable(self):
        # The chip will not leave manual mode, so the run aborts with the
        # lock held. Nothing releases it explicitly on that path: the process
        # exiting is what closes the descriptor, which is the property that
        # makes an interrupted run safe too.
        fixture = self.installed(ignore_writes=["pwm3_enable"])

        result = fixture.uninstall()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("nothing was removed", result.stderr)
        self.assertAcquires(fixture.run_writer())

    def test_a_killed_uninstall_leaves_the_lock_acquirable(self):
        # SIGKILL runs no trap and no cleanup at all. The lock is on a
        # descriptor the kernel closes anyway, so there is nothing to leave
        # behind - which is the reason it is held this way rather than by a
        # lock file with a PID in it that something has to reap.
        #
        # The kill lands while the lock is held and before the removal: the
        # race seam fires from inside the verifier's safe-state, which is
        # exactly that moment. start_new_session puts the uninstaller in a
        # session of its own, so killing its process group cannot reach the
        # test runner.
        fixture = self.installed()
        fixture.with_race_writer()
        killer = os.path.join(fixture.harness, "kill-the-uninstaller.sh")
        with open(killer, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nkill -9 -$(ps -o pgid= $$ | tr -d ' ')\n")
        os.chmod(killer, 0o755)
        fixture.extra_env["FAKE_FAN_RACE_CMD"] = killer

        result = subprocess.run(
            ["bash", os.path.join(REPO_ROOT, "scripts/uninstall.sh")],
            cwd=REPO_ROOT, env=fixture.env(), start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=180)

        self.assertNotEqual(result.returncode, 0,
                            "the uninstaller was not actually killed:\n%s"
                            % result.stdout)
        self.assertTrue(fixture.exists("usr/local/bin/qnap-tsx70-fancontrol"),
                        "the binary was already gone, so this proves nothing "
                        "about an interrupted run")
        self.assertTrue(os.path.exists(self.lock_path()),
                        "the lock file was never created, so nothing was held")
        self.assertAcquires(fixture.run_writer())


class RollbackLockTests(LockCase):
    """The rollback restores fan files, so it holds the lock for that too."""

    def failing_install(self):
        fixture = self.fixture
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-lcd"),
                      "#!/bin/sh\necho previous lcd\n", 0o755)
        fixture.add_unit(LCD_UNIT, active=True, enabled=True, mainpid=5201,
                         procs=[5201])
        fixture.add_process(5201, "python3",
                            exe=fixture.python_stub_versioned,
                            argv=["python3",
                                  os.path.join(fixture.bin_dir,
                                               "qnap-tsx70-lcd")])
        fixture.hold_port(5201, unit=LCD_UNIT)
        fixture.write(os.path.join(fixture.bin_dir, "qnap-tsx70-fancontrol"),
                      "#!/bin/sh\necho previous fan\n", 0o755)
        fixture.add_unit(FAN_UNIT, active=True, enabled=True, mainpid=6301,
                         procs=[6301], content="[Unit]\nDescription=old\n")
        fixture.write(os.path.join(fixture.state_dir,
                                   "fan-calibration.json"), VALID_CACHE)
        fixture.with_fan_controller(enable=1)
        # The LCD service comes back up but never reports active, so validate()
        # fails and the rollback runs.
        fixture.make_start_inert(LCD_UNIT)
        return fixture

    def test_a_writer_cannot_start_while_fan_files_are_restored(self):
        fixture = self.failing_install()
        fixture.with_race_writer()

        result = fixture.install("--skip-deps", "--with-fan-control")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        attempts = fixture.race_attempts()
        # Two windows: the install's own, and the rollback's fresh gate.
        self.assertGreaterEqual(len(attempts), 2,
                                "the rollback did not re-prove the handback")
        self.assertRefusedAtTheLock(attempts)
        self.assertEqual(fixture.read("usr/local/bin/qnap-tsx70-fancontrol"),
                         "#!/bin/sh\necho previous fan\n",
                         "the previous fan binary was not restored")

    def test_the_lock_comes_off_before_the_services_are_started_again(self):
        # Step 4 of the rollback restarts what was active, which for a fan
        # unit is a writer that has to take this lock itself. So the hold has
        # to end after the file restoration and before that loop - twice in
        # this run: install_files() releases the first one, the rollback's own
        # gate takes it again and releases the second.
        fixture = self.failing_install()

        result = fixture.install("--skip-deps", "--with-fan-control")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("holding the fan writer lock"), 2,
                         "the rollback did not take the lock for itself:\n%s"
                         % result.stdout)
        self.assertEqual(result.stdout.count("released the fan writer lock"), 2,
                         "a hold was left open past its last mutation:\n%s"
                         % result.stdout)
        self.assertTrue(fixture.unit_state(FAN_UNIT)["active"],
                        "the rollback left the fan service down")
        self.assertEqual(fixture.read("usr/local/bin/qnap-tsx70-fancontrol"),
                         "#!/bin/sh\necho previous fan\n")

    def test_the_lock_is_acquirable_after_the_rollback(self):
        fixture = self.failing_install()
        fixture.install("--skip-deps", "--with-fan-control")

        self.assertAcquires(fixture.run_writer())


if __name__ == "__main__":
    unittest.main()
