"""A fake root for running the shell scripts for real, without root or hardware.

The installer and the uninstaller are only interesting where they refuse to do
something: a busy serial port, a fan service that will not stop, a PID file
naming somebody else's process, a rollback after a failure in the middle. None
of that can be reached by reading the source, and none of it may be reached on
a real machine, so each script exposes a small set of documented seams and
this module supplies the other side of them:

  QNAP_TSX70_TEST_ROOT   a directory that stands in for /
  QNAP_TSX70_PROC_DIR    a directory that stands in for /proc
  QNAP_TSX70_KILL        a recorded stand-in for kill(1)
  PATH                   fake systemctl, fuser and modprobe, ahead of the real ones

The fake root is snapshotted by content and mode, which is what makes
"--dry-run changed nothing" an assertion rather than a hope.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile

from helpers import REPO_ROOT

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
"""Fake systemctl: records every call and models unit state from a file."""
import json, os, sys

state = os.environ["FAKE_SYSTEMD_STATE"]
unit_dir = os.environ.get("FAKE_UNIT_DIR", "")
proc_dir = os.environ.get("FAKE_PROC_DIR", "")
argv = sys.argv[1:]

with open(os.path.join(state, "calls.log"), "a", encoding="utf-8") as fh:
    fh.write(" ".join(argv) + "\n")


def load():
    with open(os.path.join(state, "units.json"), encoding="utf-8") as fh:
        return json.load(fh)


def save(units):
    with open(os.path.join(state, "units.json"), "w", encoding="utf-8") as fh:
        json.dump(units, fh)


def should_fail(verb, unit):
    """Fail this call, optionally only from the Nth occurrence onwards.

    daemon-reload happens twice in one migration; a test that wants the
    cleanup one to fail must be able to say so.
    """
    marker = os.path.join(state, "fail", "%s@%s" % (verb, unit))
    if not os.path.exists(marker):
        return False
    with open(marker, encoding="utf-8") as fh:
        skip = int((fh.read().strip() or "0"))
    counter = os.path.join(state, "fail", "count-%s@%s" % (verb, unit))
    seen = 0
    if os.path.exists(counter):
        with open(counter, encoding="utf-8") as fh:
            seen = int(fh.read().strip() or "0")
    seen += 1
    with open(counter, "w", encoding="utf-8") as fh:
        fh.write(str(seen))
    return seen > skip


def entry(units, unit):
    return units.get(unit)


def has_file(unit):
    return bool(unit_dir) and os.path.exists(os.path.join(unit_dir, unit))


def known(units, unit):
    item = entry(units, unit)
    if item is not None and item.get("known", True):
        return True
    return has_file(unit)


def kill_procs(units, unit):
    item = entry(units, unit) or {}
    for pid in item.get("procs", []):
        if os.path.exists(os.path.join(state, "linger", str(pid))):
            continue
        target = os.path.join(proc_dir, str(pid))
        if proc_dir and os.path.isdir(target):
            for name in os.listdir(target):
                os.remove(os.path.join(target, name))
            os.rmdir(target)


words = [a for a in argv if not a.startswith("-")]
verb = words[0] if words else ""
units = load()

if verb == "show":
    unit = words[-1]
    print((entry(units, unit) or {}).get("mainpid", 0))
    sys.exit(0)

if verb == "daemon-reload":
    if should_fail("daemon-reload", ""):
        print("Failed to reload: injected failure", file=sys.stderr)
        sys.exit(1)
    # A unit file that was deleted stops being known once systemd re-reads.
    for name in [n for n, item in units.items()
                 if item.get("from_file") and not has_file(n)]:
        units[name]["known"] = False
        units[name]["active"] = False
        units[name]["enabled"] = False
    save(units)
    sys.exit(0)

unit = words[1] if len(words) > 1 else ""
item = entry(units, unit) or {}

if verb == "is-active":
    forced = item.get("state")
    if forced:
        print(forced)
        sys.exit(0 if forced == "active" else 3)
    print("active" if item.get("active") else "inactive")
    sys.exit(0 if item.get("active") else 3)
if verb == "is-enabled":
    print("enabled" if item.get("enabled") else "disabled")
    sys.exit(0 if item.get("enabled") else 1)
if verb == "list-unit-files":
    sys.exit(0 if known(units, unit) else 1)
if verb == "status":
    print("%s - fake unit\n   Active: %s" % (unit, "active" if item.get("active") else "inactive"))
    sys.exit(0 if item.get("active") else 3)

if verb in ("stop", "start", "restart", "enable", "disable"):
    if should_fail(verb, unit):
        print("Failed to %s %s: injected failure" % (verb, unit), file=sys.stderr)
        sys.exit(1)
    item = units.setdefault(unit, {"known": True})
    if verb == "stop":
        # "stuck" models a stop that reports success while the unit stays up.
        if not os.path.exists(os.path.join(state, "stuck", unit)):
            item["active"] = False
            kill_procs(units, unit)
    elif verb in ("start", "restart"):
        # "inert" models a unit that starts, exits immediately and is never
        # reported active - what a service with a missing prerequisite does.
        if not os.path.exists(os.path.join(state, "inert", unit)):
            item["active"] = True
    elif verb == "enable":
        item["enabled"] = True
    elif verb == "disable":
        item["enabled"] = False
    save(units)
    sys.exit(0)

sys.exit(0)
'''

FAKE_FUSER = r'''#!/usr/bin/env python3
"""Fake fuser: reports the PIDs holding a port, skipping stopped units."""
import json, os, sys

state = os.environ["FAKE_SYSTEMD_STATE"]
port = [a for a in sys.argv[1:] if not a.startswith("-")]
port = port[0] if port else ""

with open(os.path.join(state, "holders.json"), encoding="utf-8") as fh:
    holders = json.load(fh)
with open(os.path.join(state, "units.json"), encoding="utf-8") as fh:
    units = json.load(fh)

live = []
for item in holders.get(port, []):
    unit = item.get("unit")
    if unit and not units.get(unit, {}).get("active"):
        continue
    live.append(str(item["pid"]))

if not live:
    sys.exit(1)
print(" ".join(live))
sys.exit(0)
'''

FAKE_KILL = r'''#!/usr/bin/env python3
"""Fake kill: records the signal and removes the fake process, if it yields."""
import os, sys

state = os.environ["FAKE_SYSTEMD_STATE"]
proc_dir = os.environ["FAKE_PROC_DIR"]
argv = sys.argv[1:]
with open(os.path.join(state, "kill.log"), "a", encoding="utf-8") as fh:
    fh.write(" ".join(argv) + "\n")

pids = [a for a in argv if not a.startswith("-")]
for pid in pids:
    if os.path.exists(os.path.join(state, "linger", pid)):
        continue
    target = os.path.join(proc_dir, pid)
    if os.path.isdir(target):
        for name in os.listdir(target):
            os.remove(os.path.join(target, name))
        os.rmdir(target)
sys.exit(0)
'''

FAKE_MODPROBE = r'''#!/usr/bin/env python3
import os, sys
state = os.environ["FAKE_SYSTEMD_STATE"]
with open(os.path.join(state, "modprobe.log"), "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(0)
'''

MUTATING_VERBS = ("stop", "start", "restart", "enable", "disable",
                  "daemon-reload")

VALID_CACHE = json.dumps({"version": 1, "channels": [1],
                          "fans": {"1": {"minstop": 90, "minstart": 120,
                                         "min_rpm": 400, "max_rpm": 1800}}})


class ShellFixture:
    """A disposable fake root plus fake system commands."""

    def __init__(self, case):
        self.base = tempfile.mkdtemp(prefix="qnap-tsx70-fixture-")
        case.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = os.path.join(self.base, "root")
        self.harness = os.path.join(self.base, "harness")
        self.bin_dir = os.path.join(self.root, "usr/local/bin")
        self.unit_dir = os.path.join(self.root, "etc/systemd/system")
        self.state_dir = os.path.join(self.root, "var/lib/qnap-tsx70")
        self.run_dir = os.path.join(self.root, "run")
        self.dev_dir = os.path.join(self.root, "dev")
        self.conf_file = os.path.join(self.root, "etc/qnap-tsx70-lcd.conf")
        self.legacy_cache = os.path.join(self.root, "etc/saturn-fan-cache.json")
        self.legacy_pid = os.path.join(self.run_dir, "saturn-fancontrol.pid")
        self.modules_file = os.path.join(self.root,
                                         "etc/modules-load.d/qnap-tsx70.conf")
        self.backup_root = os.path.join(self.root, "var/backups/qnap-tsx70")
        self.proc_dir = os.path.join(self.harness, "proc")
        self.fake_bin = os.path.join(self.harness, "bin")
        self.fake_state = os.path.join(self.harness, "state")
        self.port = os.path.join(self.dev_dir, "ttyS1")
        # Our executables are Python scripts, so the kernel reports the
        # interpreter as /proc/<pid>/exe. Kept outside the fake root so it is
        # not part of any snapshot.
        self.python_stub = os.path.join(self.harness, "usr/bin/python3")
        # What /proc/<pid>/exe actually resolves to for a `#!/usr/bin/env
        # python3` service. The scenarios below use this one, so the identity
        # checks are exercised against a versioned name by default rather than
        # against the convenient one.
        self.python_stub_versioned = os.path.join(self.harness,
                                                  "usr/bin/python3.13")

        for path in (self.bin_dir, self.unit_dir, self.run_dir, self.dev_dir,
                     os.path.join(self.root, "etc"), self.proc_dir,
                     self.fake_bin, self.fake_state,
                     os.path.join(self.fake_state, "fail"),
                     os.path.join(self.fake_state, "stuck"),
                     os.path.join(self.fake_state, "linger"),
                     os.path.join(self.fake_state, "inert"),
                     os.path.dirname(self.python_stub)):
            os.makedirs(path, exist_ok=True)
        for stub in (self.python_stub, self.python_stub_versioned):
            self._write_exec(stub, "#!/bin/sh\nexit 0\n")

        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("fuser", FAKE_FUSER),
                           ("modprobe", FAKE_MODPROBE)):
            self._write_exec(os.path.join(self.fake_bin, name), body)
        self.kill_path = os.path.join(self.fake_bin, "fake-kill")
        self._write_exec(self.kill_path, FAKE_KILL)

        self.units = {}
        self.holders = {}
        self._save_units()
        self._save_holders()
        open(os.path.join(self.fake_state, "calls.log"), "w").close()
        open(os.path.join(self.fake_state, "kill.log"), "w").close()

        # A stand-in for the panel: the scripts relax the character-device
        # check to a plain existence check under a test root, and nothing else.
        with open(self.port, "w", encoding="utf-8") as fh:
            fh.write("")

    # -- construction helpers ------------------------------------------------
    @staticmethod
    def _write_exec(path, body):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def _save_units(self):
        with open(os.path.join(self.fake_state, "units.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self.units, fh)

    def _save_holders(self):
        with open(os.path.join(self.fake_state, "holders.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self.holders, fh)

    def write(self, path, content, mode=0o644):
        full = path if os.path.isabs(path) else os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(full, mode)
        return full

    def set_active_state(self, name, state):
        """Force a literal is-active string, e.g. "activating"."""
        self.units.setdefault(name, {"known": True})["state"] = state
        self._save_units()

    def add_unit(self, name, active=False, enabled=False, mainpid=0,
                 procs=(), with_file=True, content=None):
        if with_file:
            self.write(os.path.join(self.unit_dir, name),
                       content or "[Unit]\nDescription=%s\n" % name)
        self.units[name] = {"known": True, "active": active, "enabled": enabled,
                            "mainpid": mainpid, "procs": list(procs),
                            "from_file": with_file}
        self._save_units()

    def forget_unit_file(self, name):
        """systemd still has the unit loaded, but its file is gone."""
        self.units[name]["known"] = False
        self.units[name]["from_file"] = False
        path = os.path.join(self.unit_dir, name)
        if os.path.exists(path):
            os.remove(path)
        self._save_units()

    def unit_state(self, name):
        with open(os.path.join(self.fake_state, "units.json"),
                  encoding="utf-8") as fh:
            return json.load(fh).get(name, {})

    def fail_verb(self, verb, unit, after=0):
        """Make `systemctl <verb> <unit>` exit nonzero from call `after`+1 on."""
        with open(os.path.join(self.fake_state, "fail", "%s@%s" % (verb, unit)),
                  "w", encoding="utf-8") as fh:
            fh.write(str(after))

    def make_start_inert(self, unit):
        """The unit starts without error but never becomes active."""
        open(os.path.join(self.fake_state, "inert", unit), "w").close()

    def make_stop_ineffective(self, unit):
        """`systemctl stop` succeeds but the unit stays active."""
        open(os.path.join(self.fake_state, "stuck", unit), "w").close()

    def add_process(self, pid, comm, exe=None, argv=None, linger=False):
        """Create a fake /proc entry. exe is a symlink, as the kernel makes it."""
        entry = os.path.join(self.proc_dir, str(pid))
        os.makedirs(entry, exist_ok=True)
        with open(os.path.join(entry, "comm"), "w", encoding="utf-8") as fh:
            fh.write(comm + "\n")
        with open(os.path.join(entry, "cmdline"), "wb") as fh:
            fh.write(b"\0".join(a.encode() for a in (argv or [comm])) + b"\0")
        if exe:
            link = os.path.join(entry, "exe")
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(exe, link)
        if linger:
            open(os.path.join(self.fake_state, "linger", str(pid)), "w").close()
        return entry

    def process_exists(self, pid):
        return os.path.isdir(os.path.join(self.proc_dir, str(pid)))

    def hold_port(self, pid, unit=None, port=None):
        self.holders.setdefault(port or self.port, []).append(
            {"pid": pid, "unit": unit})
        self._save_holders()

    # -- running -------------------------------------------------------------
    def env(self, **extra):
        environment = dict(os.environ)
        environment.update({
            "PATH": self.fake_bin + os.pathsep + os.environ.get("PATH", ""),
            "QNAP_TSX70_TEST_ROOT": self.root,
            "QNAP_TSX70_PROC_DIR": self.proc_dir,
            "QNAP_TSX70_KILL": self.kill_path,
            "QNAP_TSX70_SETTLE_SECS": "0",
            "QNAP_TSX70_STOP_TIMEOUT": "1",
            "FAKE_SYSTEMD_STATE": self.fake_state,
            "FAKE_UNIT_DIR": self.unit_dir,
            "FAKE_PROC_DIR": self.proc_dir,
        })
        environment.update(extra)
        return environment

    def run_script(self, script, *args, **kwargs):
        return subprocess.run(["bash", os.path.join(REPO_ROOT, script)]
                              + list(args), cwd=REPO_ROOT, env=self.env(**kwargs),
                              capture_output=True, text=True, timeout=180)

    def install(self, *args, **kwargs):
        return self.run_script("scripts/install.sh", *args, **kwargs)

    def uninstall(self, *args, **kwargs):
        return self.run_script("scripts/uninstall.sh", *args, **kwargs)

    # -- observation ---------------------------------------------------------
    def calls(self):
        with open(os.path.join(self.fake_state, "calls.log"),
                  encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]

    def mutating_calls(self):
        """Recorded systemctl calls that would have changed the system."""
        return [call for call in self.calls()
                if any(word in MUTATING_VERBS for word in call.split())]

    def kill_calls(self):
        with open(os.path.join(self.fake_state, "kill.log"),
                  encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]

    def snapshot(self, subtree=None):
        """{relative path: (mode, sha256 or 'dir')} for the whole fake root."""
        base = subtree or self.root
        state = {}
        for current, dirs, names in os.walk(base):
            dirs.sort()
            for name in sorted(dirs):
                full = os.path.join(current, name)
                state[os.path.relpath(full, base)] = (
                    stat.S_IMODE(os.lstat(full).st_mode), "dir")
            for name in sorted(names):
                full = os.path.join(current, name)
                info = os.lstat(full)
                if stat.S_ISLNK(info.st_mode):
                    digest = "link:" + os.readlink(full)
                else:
                    with open(full, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                state[os.path.relpath(full, base)] = (
                    stat.S_IMODE(info.st_mode), digest)
        return state

    def backups(self):
        if not os.path.isdir(self.backup_root):
            return []
        return sorted(os.path.join(self.backup_root, name)
                      for name in os.listdir(self.backup_root))

    def exists(self, relative):
        return os.path.exists(os.path.join(self.root, relative))

    def read(self, relative):
        with open(os.path.join(self.root, relative), encoding="utf-8") as fh:
            return fh.read()

    def mode(self, relative):
        return stat.S_IMODE(os.stat(os.path.join(self.root, relative)).st_mode)

    # -- scenarios -----------------------------------------------------------
    def with_config(self, port=None, extra=""):
        self.write(self.conf_file,
                   "serial_port = %s\n%s" % (port or self.port, extra))
        return self.conf_file

    def with_legacy(self, lcd_active=True, cache=VALID_CACHE):
        """A legacy saturn-* installation, with the LCD holding the port."""
        self.write(os.path.join(self.bin_dir, "saturn-lcd"), "#!/bin/sh\n", 0o755)
        self.write(os.path.join(self.bin_dir, "saturn-fancontrol"), "#!/bin/sh\n", 0o755)
        self.add_unit("saturn-lcd.service", active=lcd_active, enabled=True,
                      mainpid=4101, procs=[4101])
        self.add_unit("saturn-fancontrol.service", active=False, enabled=True)
        if cache is not None:
            self.write(self.legacy_cache, cache)
        if lcd_active:
            self.add_process(4101, "python3",
                             exe=self.python_stub_versioned,
                             argv=["python3", os.path.join(self.bin_dir, "saturn-lcd")])
            self.hold_port(4101, unit="saturn-lcd.service")
        return self

    def with_current_install(self, active=True):
        """A previous qnap-tsx70 install, as a reinstall would find it."""
        self.write(os.path.join(self.bin_dir, "qnap-tsx70-lcd"),
                   "#!/bin/sh\necho stale\n", 0o755)
        self.add_unit("qnap-tsx70-lcd.service", active=active, enabled=True,
                      mainpid=5201, procs=[5201])
        if active:
            self.add_process(5201, "python3",
                             exe=self.python_stub_versioned,
                             argv=["python3",
                                   os.path.join(self.bin_dir, "qnap-tsx70-lcd")])
            self.hold_port(5201, unit="qnap-tsx70-lcd.service")
        return self
