"""One writer lock per physical controller, not one per cache file.

The lock used to be placed next to whatever `--cache` named, so

    systemctl start qnap-tsx70-fancontrol           # /var/lib/.../fancontrol.lock
    qnap-tsx70-fancontrol calibrate --cache /tmp/c  # /tmp/fancontrol.lock

were two writers on the same PWM registers, each holding a lock the other
never looked at - while a calibration sweep was deliberately driving a fan
towards its stall point. The lock names the chip now, and the cache cannot
move it.

The contention tests run a second, real process. flock() is advisory and
per-open-file-description, so a same-process test would pass against a lock
scheme that does not serialise anything at all between processes.
"""

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import REPO_ROOT, load_fan, write_sysfs_tree

fan = load_fan()

# A real second process running the shipped `run` command. Only two things are
# faked, and both are seams rather than rewrites: geteuid answers 0 because
# the suite does not run as root, and the temperature input is a file in the
# fixture instead of a hwmon node under /sys.
HOLDER = r'''#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import os
import sys

os.geteuid = lambda: 0

loader = importlib.machinery.SourceFileLoader("fanctl_holder",
                                              os.environ["FAN_SOURCE"])
spec = importlib.util.spec_from_loader("fanctl_holder", loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
module.find_temp_input = lambda sensor: os.environ["FAN_SENSOR"]

sys.exit(module.main(sys.argv[1:]))
'''


class WriterLockCase(unittest.TestCase):
    """A fake chip, two cache directories, and a disposable lock root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_root = os.path.join(self.tmp.name, "run", "qnap-tsx70")
        self.real_lock_root = fan.LOCK_ROOT
        fan.LOCK_ROOT = self.lock_root
        self.addCleanup(setattr, fan, "LOCK_ROOT", self.real_lock_root)

        write_sysfs_tree(self.tmp.name, {
            "f71882fg.2592/fan3_input": "900\n",
            "f71882fg.2592/pwm3": "100\n",
            # Automatic mode, so that pwm3_enable reading 1 is proof the
            # holder wrote it rather than proof of how the file started.
            "f71882fg.2592/pwm3_enable": "2\n",
        })
        self.device = os.path.join(self.tmp.name, "f71882fg.2592")
        self.glob = os.path.join(self.tmp.name, "f71882fg.*")

        self.sensor = os.path.join(self.tmp.name, "temp1_input")
        with open(self.sensor, "w", encoding="ascii") as fh:
            fh.write("45000\n")

        self.holder_script = os.path.join(self.tmp.name, "holder.py")
        with open(self.holder_script, "w", encoding="utf-8") as fh:
            fh.write(HOLDER)

        # cmd_run and cmd_calibrate install stop handlers before taking the
        # lock, so an in-process contender would otherwise leave this process
        # with the program's handlers on SIGTERM and friends.
        for sig in fan.STOP_SIGNALS:
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

        real = os.geteuid
        os.geteuid = lambda: 0
        self.addCleanup(setattr, os, "geteuid", real)
        real_sensor = fan.find_temp_input
        fan.find_temp_input = lambda sensor: self.sensor
        self.addCleanup(setattr, fan, "find_temp_input", real_sensor)

        # A contender is supposed to be refused at the lock and never reach a
        # control loop. If it does reach one - which is what a broken lock
        # scheme looks like - it has to stop rather than run forever, or the
        # regression hangs the suite instead of failing it.
        real_read = fan.read_temp
        samples = []

        def read_temp(path):
            samples.append(path)
            if len(samples) > 5:
                raise fan.Terminated("signal SIGTERM")
            return real_read(path)

        fan.read_temp = read_temp
        self.addCleanup(setattr, fan, "read_temp", real_read)

    def cache_in(self, name, channels=(3,)):
        """A valid calibration in its own directory, as --cache would name."""
        path = os.path.join(self.tmp.name, name, "fan-calibration.json")
        fan.save_cache({"version": 1, "channels": list(channels),
                        "fans": {str(c): {"minstop": 90, "minstart": 120,
                                          "min_rpm": 400, "max_rpm": 1500}
                                 for c in channels}}, path)
        return path

    def start_holder(self, cache):
        """Run the real control loop in another process until it has the lock."""
        env = dict(os.environ)
        env.update({"FAN_SOURCE": os.path.join(REPO_ROOT,
                                               "bin/qnap-tsx70-fancontrol"),
                    "FAN_SENSOR": self.sensor,
                    "QNAP_TSX70_LOCK_DIR": self.lock_root,
                    "PYTHONUNBUFFERED": "1"})
        proc = subprocess.Popen(
            [sys.executable, self.holder_script, "run",
             "--device-glob", self.glob, "--cache", cache, "--interval", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=env, cwd=REPO_ROOT)
        self.addCleanup(self.stop_holder, proc)
        seen = []
        while True:
            line = proc.stdout.readline()
            if not line:
                self.fail("the holder exited before taking the lock:\n%s"
                          % "".join(seen))
            seen.append(line)
            if "starting" in line:
                break
        self.wait_for_manual_mode(proc, "".join(seen))
        return proc

    def wait_for_manual_mode(self, proc, so_far):
        """Wait until the holder has actually taken the channel.

        The lock is held by the time it says "starting", but stopping it
        before its first register write would exercise a different path - a
        run that never touched a channel has nothing to hand back - and that
        is not what these tests are about.
        """
        deadline = time.monotonic() + 30
        register = os.path.join(self.device, "pwm3_enable")
        while time.monotonic() < deadline:
            with open(register, encoding="ascii") as fh:
                if fh.read().strip() == str(fan.PWM_MANUAL):
                    return
            if proc.poll() is not None:
                self.fail("the holder exited before writing a register:\n%s"
                          % so_far)
            time.sleep(0.05)
        self.fail("the holder never took the channel:\n%s" % so_far)

    def stop_holder(self, proc):
        """SIGTERM, then wait. The tail of its output is kept for assertions."""
        if proc.poll() is None:
            proc.terminate()
        if proc.stdout is not None and not proc.stdout.closed:
            self.holder_output = getattr(self, "holder_output", "") \
                + proc.stdout.read()
            proc.stdout.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:          # pragma: no cover - safety
            proc.kill()
            proc.wait(timeout=30)

    def contend(self, argv):
        """Run a command that should be refused, returning (code, output)."""
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = fan.main(argv)
        return code, out.getvalue()


class LockIdentityTests(WriterLockCase):
    """What the lock is named after."""

    def test_the_lock_is_named_after_the_controller_not_the_cache(self):
        paths = fan.writer_lock_paths(self.device, root="/run/qnap-tsx70")
        self.assertEqual(paths, ["/run/qnap-tsx70/fancontrol.lock",
                                 "/run/qnap-tsx70/fancontrol-f71882fg.2592.lock"])

    def test_two_paths_to_the_same_device_give_the_same_lock(self):
        self.assertEqual(
            fan.writer_lock_paths(self.device),
            fan.writer_lock_paths(self.device + os.sep))
        self.assertEqual(
            fan.writer_lock_paths(self.device),
            fan.writer_lock_paths(os.path.join(self.device, ".")))

    def test_a_different_controller_gets_a_different_primary_lock(self):
        other = fan.writer_lock_paths("/sys/devices/platform/f71882fg.1024")
        mine = fan.writer_lock_paths(self.device)
        self.assertNotEqual(other[-1], mine[-1])
        self.assertEqual(other[0], mine[0],
                         "two controllers do not share the global lock")

    def test_an_identity_that_cannot_be_derived_falls_back_to_the_global_lock(self):
        for device in (None, "", "/", ".", "..", os.sep + os.sep):
            self.assertEqual(fan.writer_lock_paths(device, root="/run/x"),
                             ["/run/x/fancontrol.lock"], device)

    def test_a_hostile_device_name_is_sanitised_to_one_directory_entry(self):
        # The identity ends up in a path. basename() alone keeps it to one
        # component; the character filter is what keeps it a name a person can
        # read in a directory listing and a script can match - no whitespace,
        # no control characters, no shell metacharacters.
        for name in ("f71882fg.2592", "a b", "x/../../etc/passwd", "we:ird",
                     "new\nline", "$(reboot)", "a*b?c", "'quoted'"):
            identity = fan.controller_identity("/sys/devices/platform/" + name)
            self.assertNotIn("/", identity, name)
            self.assertRegex(identity, r"\A[A-Za-z0-9._-]+\Z",
                             "%r was not sanitised" % name)
            path = fan.writer_lock_paths("/sys/devices/platform/" + name,
                                         root="/run/x")[-1]
            self.assertEqual(os.path.dirname(path), "/run/x", name)

    def test_the_production_lock_root_is_the_runtime_directory(self):
        # Not /var/lib, which is where it used to live next to the cache, and
        # not a directory an operator can point somewhere else with a flag.
        self.assertEqual(fan.DEFAULT_LOCK_ROOT, "/run/qnap-tsx70")
        with open(os.path.join(REPO_ROOT, "bin/qnap-tsx70-fancontrol"),
                  encoding="utf-8") as fh:
            self.assertNotIn("--lock", fh.read())


class LockAcquisitionTests(WriterLockCase):
    """Taking, releasing, and failing to take, the set of locks."""

    def test_a_second_writer_on_the_same_controller_is_refused(self):
        held = fan.acquire_writer_locks(self.device)
        self.addCleanup(fan.release_locks, held)
        with self.assertRaises(fan.FanError) as ctx:
            fan.acquire_writer_locks(self.device)
        self.assertIn("another", str(ctx.exception))

    def test_a_writer_that_cannot_name_its_controller_still_contends(self):
        # The fallback is not a private lock: a process holding only the
        # global one blocks a process that resolved a controller, and the
        # other way round. Otherwise the fallback would be a way to race.
        held = fan.acquire_writer_locks(None)
        self.addCleanup(fan.release_locks, held)
        with self.assertRaises(fan.FanError):
            fan.acquire_writer_locks(self.device)

    def test_a_named_writer_blocks_an_unnamed_one(self):
        held = fan.acquire_writer_locks(self.device)
        self.addCleanup(fan.release_locks, held)
        with self.assertRaises(fan.FanError):
            fan.acquire_writer_locks(None)

    def test_a_refused_set_releases_the_locks_it_had_already_taken(self):
        """The global lock is taken first; a refusal after it must give it up.

        Asserted on the handles rather than by trying to take the lock again:
        CPython drops the last reference to a leaked handle as the exception
        unwinds, which closes the file and releases the flock by itself. A
        "can somebody else take it now" test therefore passes whether the
        release is written or not, and on an interpreter that collects later
        the leak would be real.
        """
        opened = []
        real_acquire = fan.acquire_lock

        def acquire_lock(path):
            handle = real_acquire(path)
            opened.append(handle)
            return handle

        fan.acquire_lock = acquire_lock
        self.addCleanup(setattr, fan, "acquire_lock", real_acquire)

        specific = fan.writer_lock_paths(self.device)[-1]
        os.makedirs(os.path.dirname(specific), exist_ok=True)
        blocker = open(specific, "w", encoding="ascii")
        self.addCleanup(blocker.close)
        fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        with self.assertRaises(fan.FanError):
            fan.acquire_writer_locks(self.device)

        self.assertTrue(opened, "no lock was ever attempted")
        for handle in opened:
            self.assertTrue(handle.closed,
                            "%s was still held after the set was refused"
                            % handle.name)
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)

    def test_the_locks_are_reusable_after_release(self):
        fan.release_locks(fan.acquire_writer_locks(self.device))
        fan.release_locks(fan.acquire_writer_locks(self.device))


class CrossProcessContentionTests(WriterLockCase):
    """Two processes, two cache directories, one chip."""

    def test_a_calibration_with_another_cache_cannot_start_under_a_run(self):
        self.start_holder(self.cache_in("service-state"))

        code, printed = self.contend(
            ["calibrate", "--yes", "--device-glob", self.glob,
             "--cache", self.cache_in("my-own-cache")])

        self.assertEqual(code, 1, printed)
        self.assertIn("another qnap-tsx70-fancontrol instance holds", printed)

    def test_a_second_run_with_another_cache_cannot_start_either(self):
        self.start_holder(self.cache_in("service-state"))

        code, printed = self.contend(
            ["run", "--device-glob", self.glob, "--interval", "0",
             "--cache", self.cache_in("elsewhere")])

        self.assertEqual(code, 1, printed)
        self.assertIn("another qnap-tsx70-fancontrol instance holds", printed)

    def test_no_lock_is_ever_created_beside_the_cache(self):
        cache = self.cache_in("service-state")
        self.start_holder(cache)

        self.assertFalse(
            os.path.exists(os.path.join(os.path.dirname(cache),
                                        "fancontrol.lock")),
            "the lock followed the cache directory again")
        self.assertTrue(os.path.exists(
            os.path.join(self.lock_root, "fancontrol-f71882fg.2592.lock")))

    def test_the_lock_is_released_when_the_holder_stops(self):
        proc = self.start_holder(self.cache_in("service-state"))
        self.stop_holder(proc)
        self.assertEqual(proc.returncode, 0, self.holder_output)

        held = fan.acquire_writer_locks(self.device)
        fan.release_locks(held)

    def test_safe_state_is_deliberately_not_serialised(self):
        # Documented emergency semantics: safe-state takes no lock, so it
        # still works while the service holds one. A recovery that can be
        # blocked by the thing it is recovering from is not a recovery.
        self.start_holder(self.cache_in("service-state"))

        code, printed = self.contend(
            ["safe-state", "--device-glob", self.glob])

        self.assertEqual(code, 0, printed)
        self.assertIn("automatic fan mode verified", printed)

    def test_a_run_that_gets_the_lock_starts_normally(self):
        # The control: without a holder, the same command reaches its loop.
        proc = self.start_holder(self.cache_in("service-state"))
        self.assertIsNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
