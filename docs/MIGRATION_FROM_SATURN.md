# Migrating from the legacy `saturn-*` installation

Earlier releases of this project installed everything under the prefix
`saturn-*`. That prefix was simply the namespace the project used before it was
generalised for the QNAP TS-x70 family — it carried no meaning beyond "this
project", and it is not a model, vendor or platform name.

Version 2.0.0 renames everything to `qnap-tsx70-*`. This document describes the
automatic migration, the equivalent manual procedure, and how to roll back.

**There are no compatibility aliases.** The old names are removed by this
migration and are not maintained in parallel. If you script around the old
paths, update those scripts.

---

## 1. What changes

| Legacy path | New path |
|---|---|
| `/usr/local/bin/saturn-lcd` | `/usr/local/bin/qnap-tsx70-lcd` |
| `/usr/local/bin/saturn-fan-calibrate` | `/usr/local/bin/qnap-tsx70-fancontrol` |
| `/etc/systemd/system/saturn-lcd.service` | `/etc/systemd/system/qnap-tsx70-lcd.service` |
| `/etc/systemd/system/saturn-fancontrol.service` | `/etc/systemd/system/qnap-tsx70-fancontrol.service` |
| `/etc/saturn-fan-cache.json` | `/var/lib/qnap-tsx70/fan-calibration.json` |
| `/run/saturn-fancontrol.pid` | removed — systemd tracks the process |
| *(none)* | `/etc/qnap-tsx70-lcd.conf` |

### Behaviour that changes at the same time

These are not cosmetic renames. Read them before migrating.

1. **Fan control is now opt-in.** A plain `install.sh` installs the LCD only.
   If you were running fan control, you must pass `--with-fan-control`.
2. **Fan control now actually runs continuously.** The legacy unit invoked the
   script without the flag that entered its control loop, so the service
   exited cleanly after calibration and the fans were left to the chip. The new
   unit runs `qnap-tsx70-fancontrol run`, which is a real loop.
3. **Fan control no longer calibrates on its own.** Calibration deliberately
   stops the fan, so it is now an explicit command. Your migrated calibration
   cache is reused, so in most cases you will not need to recalibrate.
4. **The calibration cache is re-validated on load.** Duty values are clamped
   to a safety floor, so a cache with implausibly low values will be corrected
   upward.
5. **Configuration exists.** Serial port, timings, pages and sensor names moved
   out of the source into `/etc/qnap-tsx70-lcd.conf`.

---

## 2. Automatic migration (recommended)

```bash
git pull
sudo ./scripts/install.sh                      # LCD only
# or, if you were running fan control:
sudo ./scripts/install.sh --with-fan-control
```

Preview first if you prefer:

```bash
sudo ./scripts/install.sh --dry-run
```

### What the installer does, in order

1. **Preflight.** Verifies the repository files, `python3`, systemd, the serial
   port and — for fan control — that exactly one fan controller is present. It
   also identifies which process holds the serial port, which needs `fuser` or
   `lsof`: a port whose owner cannot be read is not treated as a free one, and
   no flag overrides that. A port held by `saturn-lcd` or by an already-running
   `qnap-tsx70-lcd` is the normal case and is allowed, because the transition
   stops it. The one exception is `--no-migrate` with the legacy display
   holding the port: that run has promised not to stop it, so the conflict is
   reported here instead of after everything has been replaced. A port held by
   anything else stops the install. Nothing has been modified at this point.
2. **Record the current state.** Before the first change, the installer writes
   a transaction manifest under
   `/var/backups/qnap-tsx70/<timestamp>-<pid>/txn/`: for every path it can
   touch, whether it existed, its mode and a byte copy of its content; and for
   every unit, whether systemd had it enabled and active. Alongside it, a
   human-readable backup is laid out by kind — `bin/`, `systemd/`, `state/`
   and `config/` — plus an `enabled-units` list.
3. **Stop services for the transition.** Only what this run replaces or
   removes is stopped. The new fan unit is in scope only with
   `--with-fan-control`, since without it no fan binary is written; the legacy
   tree is in scope only when a migration is actually going to remove it, so
   `--no-migrate` leaves the legacy services running as well as their files.
   An LCD-only install therefore leaves an existing `qnap-tsx70-fancontrol`
   running, and a fan service that *was* in scope and active is started again
   once its replacement is in place.

   Fan control goes first, so its own shutdown handler hands the fans back to
   the controller's automatic mode. Each stop is verified: systemd must report
   the unit inactive and no fan-control process running an in-scope executable
   may remain. Whether a process is a writer is decided by the command it is
   running — `run`, `calibrate` and `safe-state` are writers whatever their
   options say, `status` and `validate-cache` are not, and anything the
   installer cannot classify counts as a writer. A legacy
   `/run/saturn-fancontrol.pid` is validated against the process it names
   before anything is signalled. The LCD services in scope are stopped next,
   and the serial port is confirmed released.
4. **Confirm the fans came back.** Only when this run is about to replace or
   delete a fan binary or unit — a migration that removes the legacy ones, or
   `--with-fan-control` over an existing install. Those files are the only way
   back from a fan left in manual mode, and an inactive unit with no process
   behind it is equally true of a machine whose fan is parked at a calibration
   stall duty. So the installer runs this repository's own
   `qnap-tsx70-fancontrol safe-state` against the controller and continues
   only on exit 0 — a write, a readback, and `pwmN_enable = 2` on every
   controllable channel. If it cannot be proven, **nothing fan-related is
   deleted or replaced** and the run is rolled back. An install that touches
   no fan artifact — the ordinary LCD-only one — skips this entirely and never
   writes to a fan controller. This cannot be delegated to the unit's
   `ExecStopPost=`: that line carries a `-` prefix, so systemd ignores its
   exit status by design.
5. **Migrate the cache.** The legacy cache is validated by the fan binary
   itself and only then copied to the new location. A cache that does not
   validate is **left exactly where it is** and reported; it is not deleted
   later either.
6. **Install.** Binaries, unit files and the config are copied from the
   versioned files in the repository.
7. **Enable and start.** The LCD service is enabled and started. Fan control,
   if requested, is installed, and it is enabled **only** when a valid
   calibration cache is already present — otherwise the unit stays disabled
   and the installer prints the calibrate-then-enable sequence.
8. **Validate.** Confirms the LCD service is active and runs its preflight.
9. **Remove legacy artifacts** — only after validation passes, only after
   re-confirming no fan-control process is running, and only with step 4's
   verdict on record.
10. **Post-cleanup validation.** Confirms the new service is still active and
   the legacy units are gone.

A failure in **any** step from 3 onwards — including during legacy cleanup or
post-cleanup validation — rolls the whole thing back from the manifest: every
recorded path is restored to its previous content and mode, paths this run
created are removed, paths that already existed are put back rather than
deleted, and every unit is returned to the enablement and activity it had
before. The backup and the manifest are kept, and their paths are printed.

`--dry-run` performs none of this. It prints what each step would do,
including what a rollback would restore, and changes nothing at all.

### An existing config is never overwritten

If `/etc/qnap-tsx70-lcd.conf` already exists it is kept as-is. New keys added
by a later version appear in `config/qnap-tsx70-lcd.conf.example`; compare the
two after upgrading.

---

## 3. Manual migration

If you prefer to do it yourself, or the automatic path failed, paste the whole
block below into one `bash` session and run it in order. It is a single script,
not a menu: step 0 defines the guard rails, and every step that deletes
something calls them first. A guard that refuses exits the script there, before
the deletion, with nothing removed.

The guards are not decoration. Deleting a fan binary or its unit while a
writer is alive leaves a process driving the PWM registers that systemd no
longer knows about and whose `safe-state` executable is gone. That is why a
failed or ineffective stop has to stop the whole procedure rather than print a
line and continue.

```bash
# 0. Guard rails. Everything below calls these; nothing below ignores them.
LEGACY_FAN_UNITS="saturn-fancontrol.service saturn-fan.service"
LEGACY_LCD_UNITS="saturn-lcd.service"
FAN_UNITS="$LEGACY_FAN_UNITS qnap-tsx70-fancontrol.service"
LEGACY_BINS="saturn-lcd saturn-fancontrol saturn-fan saturn-fan-calibrate"
# Executables that may be driving the fans, old prefix and new.
FAN_NAMES="saturn-fancontrol saturn-fan saturn-fan-calibrate qnap-tsx70-fancontrol"
PORT=/dev/ttyS1

abort() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

# `systemctl is-active` exits nonzero for a unit that is still starting or
# still stopping, so the string decides and not the exit status. Removing
# files under either state is the hazard this whole section is about.
unit_is_active() {
    local state
    state="$(systemctl is-active "$1" 2>/dev/null || true)"
    case "$state" in
        active|activating|reloading|refreshing|deactivating) return 0 ;;
        *) return 1 ;;
    esac
}

# Identity comes from comm, or from the command word - argv[0], or argv[1]
# when an interpreter runs the script. A name further along a command line is
# a mention, not an identity. Any fan-control process counts here, a read-only
# `status` you left open in another terminal included: close it rather than
# working around this.
fan_command_words() {
    [ -r "$1/comm" ] && tr -d '\n' < "$1/comm" && printf '\n'
    [ -r "$1/cmdline" ] && tr '\0' '\n' < "$1/cmdline" | head -2
    return 0
}

fan_processes() {
    local entry word
    for entry in /proc/[0-9]*; do
        [ -d "$entry" ] || continue
        while IFS= read -r word; do
            case " $FAN_NAMES " in
                *" ${word##*/} "*) printf '%s ' "${entry##*/}"; break ;;
            esac
        done < <(fan_command_words "$entry")
    done
    return 0
}

require_inactive() {
    local unit
    for unit in "$@"; do
        if unit_is_active "$unit"; then
            abort "$unit is still active; nothing has been removed"
        fi
    done
}

require_no_fan_writer() {
    local remaining
    remaining="$(fan_processes)"
    if [ -n "$remaining" ]; then
        abort "fan-control process(es) still running: $remaining;
       nothing has been removed. Stop them, then re-run from step 0."
    fi
}

require_quiescent() {
    require_inactive $FAN_UNITS $LEGACY_LCD_UNITS
    require_no_fan_writer
}

# The repository copy, not an installed one: the verdict has to come from the
# version being shipped, because a legacy binary's idea of "handed back" is
# exactly what is not being trusted here.
FANCTL="python3 bin/qnap-tsx70-fancontrol"

# An inactive unit and an empty process table are both equally true of a
# machine whose fan is parked at a calibration stall duty with pwmN_enable
# still reading 1. Neither says anything about the PWM registers. `safe-state`
# writes full duty, writes automatic mode, reads the mode back, and exits 0
# only if it read 2 on every controllable channel - so its exit status, and
# nothing else about it, is the evidence. Note that `--dry-run` deliberately
# exits 3 here: a preview is not a hardware check and cannot earn an `rm`.
require_fan_handback() {
    $FANCTL safe-state && return 0
    abort "the fans could not be confirmed back in the controller's automatic
       mode. NOTHING has been removed: the fan binaries and units still on
       this machine are the only way to put them back. If the driver is not
       loaded, 'sudo modprobe f71882fg' first; then check
       '$FANCTL status' and re-run from step 0."
}

# 1. Stop every fan writer first, then the display. A stop that fails aborts
#    here: step 8 deletes the binary these units run.
for unit in $FAN_UNITS $LEGACY_LCD_UNITS; do
    if unit_is_active "$unit"; then
        sudo systemctl stop "$unit" || abort "systemctl stop $unit failed"
    fi
    # Disabling only affects the next boot, so a unit that was never enabled
    # or is not known at all may fail here without endangering anything.
    sudo systemctl disable "$unit" >/dev/null 2>&1 || true
done

# 2. Verify. This is a gate, not a report: a `systemctl stop` can return 0 and
#    leave the unit up, and a writer can outlive the unit that supervised it.
for unit in $FAN_UNITS $LEGACY_LCD_UNITS; do
    printf '%s: %s\n' "$unit" "$(systemctl is-active "$unit" 2>&1)"
done
require_quiescent
echo 'no fan writer remains'

# 3. The serial port has to be free, and who holds it has to be answerable.
#    An unverifiable port is not a free one, so a host with neither tool stops
#    here instead of guessing.
if command -v fuser >/dev/null 2>&1; then
    holders="$(sudo fuser "$PORT" 2>/dev/null | tr -cd '0-9 ')"
elif command -v lsof >/dev/null 2>&1; then
    holders="$(sudo lsof -t -- "$PORT" 2>/dev/null | tr '\n' ' ')"
else
    abort "neither fuser nor lsof is installed, so who holds $PORT cannot be
       checked. Install psmisc or lsof first."
fi
[ -z "${holders// /}" ] || abort "$PORT is still held by: $holders"
echo 'serial port free'

# 4. If /run/saturn-fancontrol.pid exists, identify what it names before
#    acting on it. Do not signal a PID you have not identified: PIDs are
#    reused, and the one in a stale file may now be something else entirely.
pid=""
if [ -f /run/saturn-fancontrol.pid ]; then
    pid="$(tr -cd '0-9' < /run/saturn-fancontrol.pid)"
fi
if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
    sudo readlink -f "/proc/$pid/exe" || true
    tr '\0' ' ' < "/proc/$pid/cmdline"; echo
    abort "/run/saturn-fancontrol.pid names live pid $pid; identify it, stop it
       yourself, and re-run from step 0"
fi

# 5. Back everything up, one directory per kind so a restore can be precise.
#    A backup that did not happen must not be mistaken for one, so each copy
#    is checked rather than discarded with `|| true`.
BACKUP=/var/backups/qnap-tsx70/manual
sudo mkdir -p "$BACKUP"/bin "$BACKUP"/systemd "$BACKUP"/state
for unit in $LEGACY_FAN_UNITS $LEGACY_LCD_UNITS; do
    [ -f "/etc/systemd/system/$unit" ] || continue
    sudo cp -a "/etc/systemd/system/$unit" "$BACKUP"/systemd/ \
        || abort "could not back up $unit"
done
for binary in $LEGACY_BINS; do
    [ -f "/usr/local/bin/$binary" ] || continue
    sudo cp -a "/usr/local/bin/$binary" "$BACKUP"/bin/ \
        || abort "could not back up $binary"
done
if [ -f /etc/saturn-fan-cache.json ]; then
    sudo cp -a /etc/saturn-fan-cache.json "$BACKUP"/state/ \
        || abort "could not back up the calibration cache"
fi
ls -R "$BACKUP"

# 6. Migrate the calibration cache, and record whether that actually happened.
#    MIGRATED_CACHE is what step 9 keys off. A valid calibration that was
#    already installed is not evidence that this legacy file was migrated - it
#    is somebody else's measurement, and deleting the legacy file on its
#    strength destroys the only copy outside the backup.
MIGRATED_CACHE=0
sudo mkdir -p /var/lib/qnap-tsx70
if [ ! -f /etc/saturn-fan-cache.json ]; then
    echo 'no legacy calibration cache to migrate'
elif python3 bin/qnap-tsx70-fancontrol validate-cache \
        --cache /var/lib/qnap-tsx70/fan-calibration.json >/dev/null 2>&1; then
    echo 'a valid calibration is already installed; not overwriting it,'
    echo 'and the legacy file is therefore NOT migrated and NOT deleted'
elif python3 bin/qnap-tsx70-fancontrol validate-cache \
        --cache /etc/saturn-fan-cache.json >/dev/null 2>&1; then
    sudo install -m 0644 /etc/saturn-fan-cache.json \
        /var/lib/qnap-tsx70/fan-calibration.json \
        || abort "could not install the migrated cache"
    MIGRATED_CACHE=1
    echo 'migrated /etc/saturn-fan-cache.json'
else
    echo 'the legacy cache does not validate; keeping it exactly where it is'
fi

# 7. Install the new files from the repository. An existing configuration is
#    kept: the example is a starting point, not an upgrade.
sudo install -m 0755 bin/qnap-tsx70-lcd /usr/local/bin/qnap-tsx70-lcd
[ -f /etc/qnap-tsx70-lcd.conf ] \
    || sudo install -m 0644 config/qnap-tsx70-lcd.conf.example \
                    /etc/qnap-tsx70-lcd.conf
sudo install -m 0644 systemd/qnap-tsx70-lcd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable qnap-tsx70-lcd.service
sudo systemctl start qnap-tsx70-lcd.service
systemctl is-active qnap-tsx70-lcd.service \
    || abort "qnap-tsx70-lcd did not start; nothing legacy has been removed"
# Its own preflight. Warnings here are informational, so they do not abort.
sudo /usr/local/bin/qnap-tsx70-lcd --config /etc/qnap-tsx70-lcd.conf --check \
    || true

# 8. Only once that works, remove the legacy files - and only once the guards
#    still agree, because the new service has been started since step 2 and
#    something could have come back up with it. The handback is checked last,
#    after quiescence: asking the chip to take the fans back while a writer is
#    still driving them is answered by whichever of the two wrote last.
require_quiescent
require_fan_handback
for unit in $LEGACY_FAN_UNITS $LEGACY_LCD_UNITS; do
    sudo rm -f "/etc/systemd/system/$unit"
done
for binary in $LEGACY_BINS; do
    sudo rm -f "/usr/local/bin/$binary"
done
sudo rm -f /run/saturn-fancontrol.pid

# 9. The cache is deliberately not in that list. It goes only if step 6
#    migrated this exact file and the installed copy still validates.
if [ "$MIGRATED_CACHE" -eq 1 ] && python3 bin/qnap-tsx70-fancontrol \
        validate-cache --cache /var/lib/qnap-tsx70/fan-calibration.json \
        >/dev/null 2>&1; then
    sudo rm -f /etc/saturn-fan-cache.json
    echo 'removed /etc/saturn-fan-cache.json (migrated and validated)'
else
    echo 'keeping /etc/saturn-fan-cache.json: step 6 did not migrate it'
fi
sudo systemctl daemon-reload
echo 'manual migration complete'
```

For fan control, additionally install `bin/qnap-tsx70-fancontrol` and
`systemd/qnap-tsx70-fancontrol.service`, then read
[FAN_CONTROL.md](FAN_CONTROL.md) before starting the service.

---

## 4. Rolling back

The automatic rollback runs by itself on a failed install. To go back
deliberately afterwards, use the backup directory the installer reported.

If the failed install had already restarted fan control, the rollback proves
the handback again before it restores or removes a fan binary or unit: it stops
every fan unit that may be running, checks that no fan writer survived, and
runs `safe-state` once more. It reuses nothing from the start of the run. If
any of that fails, it rolls back everything else, leaves every fan binary and
unit exactly as the failed install wrote them, prints `INCOMPLETE ROLLBACK`
with the paths it left alone — also written to `txn/fan-rollback-skipped` in
the backup directory — and exits nonzero. Recover the fans first, then restore
those paths by hand with the block below.

This block deletes the new binaries and units, so it carries the same guards
as the manual migration, aimed at the artifacts it is the one removing: the
`qnap-tsx70-*` fan service and any process still running its binary. A stop
that fails or does not take effect aborts before the first `rm`.

The backup is laid out by kind, so each destination gets only what belongs
there. A flat `cp "$BACKUP"/saturn-* /usr/local/bin/` would also copy unit
files and the cache into the binary directory.

```bash
BACKUP=/var/backups/qnap-tsx70/<timestamp>-<pid>

NEW_UNITS="qnap-tsx70-fancontrol.service qnap-tsx70-lcd.service"
NEW_FAN_NAMES="qnap-tsx70-fancontrol"

abort() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

unit_is_active() {
    local state
    state="$(systemctl is-active "$1" 2>/dev/null || true)"
    case "$state" in
        active|activating|reloading|refreshing|deactivating) return 0 ;;
        *) return 1 ;;
    esac
}

fan_command_words() {
    [ -r "$1/comm" ] && tr -d '\n' < "$1/comm" && printf '\n'
    [ -r "$1/cmdline" ] && tr '\0' '\n' < "$1/cmdline" | head -2
    return 0
}

fan_processes() {
    local entry word
    for entry in /proc/[0-9]*; do
        [ -d "$entry" ] || continue
        while IFS= read -r word; do
            case " $NEW_FAN_NAMES " in
                *" ${word##*/} "*) printf '%s ' "${entry##*/}"; break ;;
            esac
        done < <(fan_command_words "$entry")
    done
    return 0
}

# Fan control first: its own shutdown handler is what hands the fans back to
# the chip, and its binary is about to be deleted.
for unit in $NEW_UNITS; do
    if unit_is_active "$unit"; then
        sudo systemctl stop "$unit" || abort "systemctl stop $unit failed"
    fi
    sudo systemctl disable "$unit" >/dev/null 2>&1 || true
done
for unit in $NEW_UNITS; do
    if unit_is_active "$unit"; then
        abort "$unit is still active; nothing has been removed"
    fi
done
remaining="$(fan_processes)"
[ -z "$remaining" ] || abort "fan-control process(es) still running: $remaining;
       nothing has been removed"

# The next two lines delete the fan binary and its unit - the only way back
# from a fan left in manual mode. A stopped unit and an empty process table do
# not prove the chip took the fans back; only a write and a readback do, which
# is what `safe-state` exits 0 for. The repository copy is the authority, and
# `--dry-run` is not an answer here: it exits 3 precisely so it cannot be
# mistaken for one.
FANCTL="python3 bin/qnap-tsx70-fancontrol"
$FANCTL safe-state || abort "the fans could not be confirmed back in the
       controller's automatic mode. Nothing has been removed. Recover first -
       'sudo modprobe f71882fg' if the driver is not loaded - and re-run."

sudo rm -f /etc/systemd/system/qnap-tsx70-lcd.service \
           /etc/systemd/system/qnap-tsx70-fancontrol.service
sudo rm -f /usr/local/bin/qnap-tsx70-lcd /usr/local/bin/qnap-tsx70-fancontrol

# Units, binaries and state each come from their own directory.
sudo cp -a "$BACKUP"/systemd/saturn-*.service /etc/systemd/system/ 2>/dev/null || true
for binary in saturn-lcd saturn-fan-calibrate saturn-fancontrol saturn-fan; do
    [ -f "$BACKUP/bin/$binary" ] \
        && sudo cp -a "$BACKUP/bin/$binary" "/usr/local/bin/$binary"
done
sudo cp -a "$BACKUP"/state/saturn-fan-cache.json /etc/ 2>/dev/null || true

sudo systemctl daemon-reload
# Re-enable whatever was enabled before; the list is in "$BACKUP/enabled-units".
cat "$BACKUP/enabled-units"
```

The exact prior state — including the mode of each file and which units were
active as well as enabled — is recorded in `"$BACKUP"/txn/manifest.tsv` and
`"$BACKUP"/txn/units.tsv`, with the original bytes under `"$BACKUP"/txn/files/`.
That is what the installer's own automatic rollback replays.

Note that the legacy fan-control service had the defects described above,
including never entering its control loop. Rolling back restores that
behaviour too.

---

## 5. Verifying the migration

```bash
systemctl is-active qnap-tsx70-lcd          # expect: active
sudo qnap-tsx70-lcd --check                 # expect: all ok, or explained warnings
journalctl -u qnap-tsx70-lcd -n 30

# Nothing legacy should remain, with one documented exception: a calibration
# cache that did not validate is deliberately kept at its original path.
ls /etc/systemd/system/saturn-* /usr/local/bin/saturn-* /etc/saturn-fan-cache.json 2>&1
# expect: No such file or directory, unless the installer reported that it
# kept /etc/saturn-fan-cache.json because it did not validate

# If you migrated fan control:
qnap-tsx70-fancontrol status
```

The display should resume its page rotation within a few seconds.

---

## 6. Troubleshooting

**The LCD service will not start after migration.**
Check the port: `sudo qnap-tsx70-lcd --check`. If a legacy process is still
holding it, `sudo fuser /dev/ttyS1` will show it. The most common cause is a
legacy unit that was not stopped; `systemctl list-units 'saturn-*'` finds it.

**The display shows the old text and never updates.**
The panel holds its last contents. A stale writer is still running, or the new
service failed to start. Check both.

**Fan control will not start.**
It is never started by the installer, and it is only *enabled* when a valid
calibration cache is already in place — an enabled service with no calibration
fails at every boot until systemd's start limit stops trying. If your cache
migrated, `qnap-tsx70-fancontrol status` shows it and you can
`systemctl start qnap-tsx70-fancontrol`. If it did not migrate, read
[FAN_CONTROL.md](FAN_CONTROL.md) and then:

```bash
sudo qnap-tsx70-fancontrol calibrate --yes    # stops the fan; supervise it
sudo systemctl enable --now qnap-tsx70-fancontrol
```

**The installer refuses to start because something holds the serial port.**
That is deliberate for a process it does not recognise. `sudo fuser -v
/dev/ttyS1` names it. Stop it, or — if you know what it is and will stop it
yourself — re-run with `--allow-serial-owner PID`. The installer still never
signals that process, and still refuses to continue unless the port is free
by the time the service needs it. `--force` does not cover this case.

**The installer says who holds the serial port cannot be determined.**
Neither `fuser` nor `lsof` is installed, so there is nothing to ask. Install
one — `apt-get install psmisc` provides `fuser` — and re-run. Neither
`--force` nor `--allow-serial-owner` covers this: without a way to read the
port, a named PID cannot be confirmed to hold it, to exist, or to have let go
by the time the LCD service opens the port, so accepting either flag would
only mean printing a check that never happened.

**The installer refuses because `--no-migrate` and the legacy display clash.**
The legacy `saturn-lcd` holds the panel's serial port, and `--no-migrate` is a
promise to leave the legacy installation — that process included — exactly as
it is, so the new service could never open the port. Stop the legacy service
yourself, or drop `--no-migrate` and let the migration stop it after the
backup exists. This is reported during preflight, before anything is changed.

**I want to start over.**
`sudo ./scripts/uninstall.sh --purge` removes everything including config and
state, then install again.
