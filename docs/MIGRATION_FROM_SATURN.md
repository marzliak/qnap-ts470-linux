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
   port, and — for fan control — that exactly one fan controller is present.
   Nothing has been modified at this point; a failure here aborts cleanly.
2. **Backup.** Copies legacy units, binaries, the calibration cache and any
   existing config to `/var/backups/qnap-tsx70/<timestamp>/`, and records which
   legacy units were enabled.
3. **Stop legacy services.** Fan control is stopped first, so its own shutdown
   handler hands the fans back to the controller's automatic mode before
   anything else moves.
4. **Migrate the cache.** The legacy cache is parsed as JSON and only then
   copied to the new location. An unparseable cache is left alone and reported.
5. **Install.** Binaries, unit files and the config are copied from the
   versioned files in the repository.
6. **Enable and start.** The LCD service is enabled and started. Fan control,
   if requested, is enabled but **not** started.
7. **Validate.** Confirms the LCD service is active and runs its preflight.
8. **Remove legacy artifacts** — only after validation passes.

If any step between 5 and 7 fails, the installer rolls back automatically:
legacy units and binaries are restored from the backup, the new units are
removed, and whatever was enabled before is enabled and started again.

### An existing config is never overwritten

If `/etc/qnap-tsx70-lcd.conf` already exists it is kept as-is. New keys added
by a later version appear in `config/qnap-tsx70-lcd.conf.example`; compare the
two after upgrading.

---

## 3. Manual migration

If you prefer to do it yourself, or the automatic path failed:

```bash
# 1. Stop fan control first, then the display.
sudo systemctl disable --now saturn-fancontrol.service 2>/dev/null || true
sudo systemctl disable --now saturn-lcd.service

# 2. Back everything up.
sudo mkdir -p /var/backups/qnap-tsx70/manual
sudo cp -a /etc/systemd/system/saturn-*.service \
           /usr/local/bin/saturn-* \
           /etc/saturn-fan-cache.json \
           /var/backups/qnap-tsx70/manual/ 2>/dev/null || true

# 3. Move the calibration cache, after checking it is valid JSON.
sudo mkdir -p /var/lib/qnap-tsx70
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' /etc/saturn-fan-cache.json \
  && sudo install -m 0644 /etc/saturn-fan-cache.json \
       /var/lib/qnap-tsx70/fan-calibration.json

# 4. Install the new files from the repository.
sudo install -m 0755 bin/qnap-tsx70-lcd /usr/local/bin/qnap-tsx70-lcd
sudo install -m 0644 config/qnap-tsx70-lcd.conf.example /etc/qnap-tsx70-lcd.conf
sudo install -m 0644 systemd/qnap-tsx70-lcd.service /etc/systemd/system/

# 5. Start and verify.
sudo systemctl daemon-reload
sudo systemctl enable --now qnap-tsx70-lcd
systemctl is-active qnap-tsx70-lcd
sudo qnap-tsx70-lcd --check

# 6. Only once that works, remove the legacy files.
sudo rm -f /etc/systemd/system/saturn-*.service \
           /usr/local/bin/saturn-lcd \
           /usr/local/bin/saturn-fan-calibrate \
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

```bash
BACKUP=/var/backups/qnap-tsx70/<timestamp>

sudo systemctl disable --now qnap-tsx70-lcd qnap-tsx70-fancontrol 2>/dev/null || true
sudo rm -f /etc/systemd/system/qnap-tsx70-*.service
sudo rm -f /usr/local/bin/qnap-tsx70-*

sudo cp -a "$BACKUP"/saturn-*.service /etc/systemd/system/ 2>/dev/null || true
sudo cp -a "$BACKUP"/saturn-* /usr/local/bin/ 2>/dev/null || true
sudo cp -a "$BACKUP"/saturn-fan-cache.json /etc/ 2>/dev/null || true

sudo systemctl daemon-reload
# Re-enable whatever was enabled before; the list is in "$BACKUP/enabled-units".
cat "$BACKUP/enabled-units"
```

Note that the legacy fan-control service had the defects described above,
including never entering its control loop. Rolling back restores that
behaviour too.

---

## 5. Verifying the migration

```bash
systemctl is-active qnap-tsx70-lcd          # expect: active
sudo qnap-tsx70-lcd --check                 # expect: all ok, or explained warnings
journalctl -u qnap-tsx70-lcd -n 30

# Nothing legacy should remain:
ls /etc/systemd/system/saturn-* /usr/local/bin/saturn-* /etc/saturn-fan-cache.json 2>&1
# expect: No such file or directory

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
It is enabled but not started on purpose, and it needs a calibration. If your
cache migrated, `qnap-tsx70-fancontrol status` shows it; start the service
normally. If it did not migrate, read [FAN_CONTROL.md](FAN_CONTROL.md) — the
calibration procedure stops your fan and should not be run unattended.

**I want to start over.**
`sudo ./scripts/uninstall.sh --purge` removes everything including config and
state, then install again.
