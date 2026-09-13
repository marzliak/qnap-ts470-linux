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
   also identifies which process holds the serial port. A port held by
   `saturn-lcd` or by an already-running `qnap-tsx70-lcd` is the normal case
   and is allowed; a port held by anything else stops the install. Nothing has
   been modified at this point.
2. **Record the current state.** Before the first change, the installer writes
   a transaction manifest under
   `/var/backups/qnap-tsx70/<timestamp>-<pid>/txn/`: for every path it can
   touch, whether it existed, its mode and a byte copy of its content; and for
   every unit, whether systemd had it enabled and active. Alongside it, a
   human-readable backup is laid out by kind — `bin/`, `systemd/`, `state/`
   and `config/` — plus an `enabled-units` list.
3. **Stop services for the transition.** Fan control first, so its own
   shutdown handler hands the fans back to the controller's automatic mode.
   Each stop is verified: systemd must report the unit inactive and no known
   fan-control process may remain. A legacy `/run/saturn-fancontrol.pid` is
   validated against the process it names before anything is signalled. The
   LCD services are stopped next, and the serial port is confirmed released.
4. **Migrate the cache.** The legacy cache is validated by the fan binary
   itself and only then copied to the new location. A cache that does not
   validate is **left exactly where it is** and reported; it is not deleted
   later either.
5. **Install.** Binaries, unit files and the config are copied from the
   versioned files in the repository.
6. **Enable and start.** The LCD service is enabled and started. Fan control,
   if requested, is installed, and it is enabled **only** when a valid
   calibration cache is already present — otherwise the unit stays disabled
   and the installer prints the calibrate-then-enable sequence.
7. **Validate.** Confirms the LCD service is active and runs its preflight.
8. **Remove legacy artifacts** — only after validation passes, and only after
   re-confirming no fan-control process is running.
9. **Post-cleanup validation.** Confirms the new service is still active and
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

If you prefer to do it yourself, or the automatic path failed:

```bash
# 1. Stop every fan writer first, then the display. Both legacy fan unit
#    names are recognised by the installer, so both have to be handled here.
for unit in saturn-fancontrol.service saturn-fan.service; do
    sudo systemctl disable --now "$unit" 2>/dev/null || true
done
sudo systemctl disable --now saturn-lcd.service 2>/dev/null || true

# 2. Verify all three really stopped. Do not continue while any is active.
for unit in saturn-fancontrol.service saturn-fan.service saturn-lcd.service; do
    printf '%s: %s\n' "$unit" "$(systemctl is-active "$unit" 2>&1)"
done   # expect: inactive, failed or unknown for all three

# 3. Verify no fan process survived its unit, and that the serial port is
#    free. A process still writing PWM must be stopped before you continue.
pgrep -a -f 'saturn-fan' || echo 'no legacy fan process'
sudo fuser /dev/ttyS1 || echo 'serial port free'

# 4. If /run/saturn-fancontrol.pid exists, check what it names before acting.
#    Do not signal the PID unless it really is the legacy fan controller.
pid="$(cat /run/saturn-fancontrol.pid 2>/dev/null)"
[ -n "$pid" ] && sudo readlink -f "/proc/$pid/exe" 2>/dev/null

# 5. Back everything up, one directory per kind so a restore can be precise.
sudo mkdir -p /var/backups/qnap-tsx70/manual/{bin,systemd,state}
sudo cp -a /etc/systemd/system/saturn-*.service \
           /var/backups/qnap-tsx70/manual/systemd/ 2>/dev/null || true
for binary in saturn-lcd saturn-fan-calibrate saturn-fancontrol saturn-fan; do
    [ -f "/usr/local/bin/$binary" ] \
        && sudo cp -a "/usr/local/bin/$binary" \
                      /var/backups/qnap-tsx70/manual/bin/
done
sudo cp -a /etc/saturn-fan-cache.json \
           /var/backups/qnap-tsx70/manual/state/ 2>/dev/null || true

# 6. Move the calibration cache, but only if the fan binary accepts it.
sudo mkdir -p /var/lib/qnap-tsx70
python3 bin/qnap-tsx70-fancontrol validate-cache \
        --cache /etc/saturn-fan-cache.json \
  && sudo install -m 0644 /etc/saturn-fan-cache.json \
       /var/lib/qnap-tsx70/fan-calibration.json
# If it does not validate, keep it where it is. Do not delete it in step 8.

# 7. Install the new files from the repository.
sudo install -m 0755 bin/qnap-tsx70-lcd /usr/local/bin/qnap-tsx70-lcd
sudo install -m 0644 config/qnap-tsx70-lcd.conf.example /etc/qnap-tsx70-lcd.conf
sudo install -m 0644 systemd/qnap-tsx70-lcd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qnap-tsx70-lcd
systemctl is-active qnap-tsx70-lcd
sudo qnap-tsx70-lcd --check

# 8. Only once that works, remove the legacy files. The cache line belongs
#    here only if step 6 actually migrated it.
sudo rm -f /etc/systemd/system/saturn-*.service \
           /usr/local/bin/saturn-lcd \
           /usr/local/bin/saturn-fan-calibrate \
           /usr/local/bin/saturn-fancontrol \
           /usr/local/bin/saturn-fan \
           /etc/saturn-fan-cache.json \
           /run/saturn-fancontrol.pid
sudo systemctl daemon-reload
```

For fan control, additionally install `bin/qnap-tsx70-fancontrol` and
`systemd/qnap-tsx70-fancontrol.service`, then read
[FAN_CONTROL.md](FAN_CONTROL.md) before starting the service.

---

## 4. Rolling back

The automatic rollback runs by itself on a failed install. To go back
deliberately afterwards, use the backup directory the installer reported:

The backup is laid out by kind, so each destination gets only what belongs
there. A flat `cp "$BACKUP"/saturn-* /usr/local/bin/` would also copy unit
files and the cache into the binary directory.

```bash
BACKUP=/var/backups/qnap-tsx70/<timestamp>-<pid>

sudo systemctl disable --now qnap-tsx70-lcd qnap-tsx70-fancontrol 2>/dev/null || true
sudo rm -f /etc/systemd/system/qnap-tsx70-*.service
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

**I want to start over.**
`sudo ./scripts/uninstall.sh --purge` removes everything including config and
state, then install again.
