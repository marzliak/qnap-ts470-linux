# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — installer, uninstaller and diagnostics safety

Post-merge review of [#2] found failure paths the 234 passing tests did not
cover. None of this had reached hardware; the fixes protect the migration,
reinstall, rollback and diagnostic paths before it does.

- **Migration and reinstall are no longer blocked by their own serial port.**
  Preflight now identifies who holds the port. A legacy `saturn-lcd` or an
  already-running `qnap-tsx70-lcd` is the planned transition and is stopped
  after the backup exists; the port is then confirmed released before the new
  service opens it. An unrecognised process still stops the install, and
  `--force` deliberately does not override that — the new
  `--allow-serial-owner PID` is the documented, narrow escape hatch, and it
  never signals the process it names.
- **A fan service that will not stop aborts the run.** Both the installer and
  the uninstaller now require systemd to report the unit inactive *and* no
  known fan-control process to remain before any artifact is removed. Neither
  claims the chip is back in automatic mode without confirming it.
- **The legacy PID file is validated before anything is signalled.** Malformed,
  empty, stale and reused PIDs are each handled explicitly; identity is checked
  against `/proc/<pid>/comm`, the resolved executable and — for interpreted
  scripts — an absolute script path in argv. A process that ignores `SIGTERM`
  aborts the install rather than being killed, because `SIGKILL` would skip the
  handler that frees the fans.
- **`--dry-run` is non-mutating everywhere, including rollback.** `rollback()`
  used to call `systemctl`, `rm` and `cp` directly, so a trapped failure during
  a dry run could change the host for real.
- **Rollback is transactional.** A manifest records the previous existence,
  content, mode and unit enablement/activity of every path the run can touch,
  so a pre-existing new-name artifact is restored instead of deleted and only
  paths this run created are removed. The trap stays armed through legacy
  cleanup, `daemon-reload` and post-cleanup validation, and is disarmed inside
  `rollback()` itself so a failing restore cannot re-enter the handler.
- **An invalid legacy calibration cache is preserved**, at its original path
  and in the backup, and the cleanup summary no longer claims it was removed.
- **Fan control is not enabled without a valid calibration.** The unit also
  carries `ConditionPathExists=` as defence in depth.
- **One source of truth for configuration.** `qnap-tsx70-lcd --print-config KEY`
  returns the effective value, and the shell scripts use it instead of their
  own `sed`, which disagreed with the runtime about quotes and inline comments.
  `qnap-tsx70-fancontrol validate-cache` does the same for the calibration
  schema.
- **Diagnostics.** IPv6 redaction now covers compressed, expanded, CIDR,
  zone-ID and IPv4-mapped forms through a tested `ipaddress`-based filter
  (`scripts/redact.py`) rather than one regex, while leaving timestamps and
  ordinary colon-separated text alone. Writing with `-o` is a transaction:
  temporary file in the destination directory, both collection and redaction
  must succeed, mode 0600, atomic rename, and only then a success message.
  Invalid directories, a directory as the target, permission failures and a
  missing argument all exit nonzero and leave nothing behind.
- **Documentation.** The rollback commands no longer copy unit files and the
  cache into `/usr/local/bin`; the backup is laid out by kind. Manual migration
  covers `saturn-fancontrol.service`, `saturn-fan.service` and the LCD, and
  verifies each. The documented rollback is executed against a fixture backup
  tree by the test suite, so it cannot drift from the layout again.

### Added

- `scripts/redact.py`, the diagnostic redaction filter, and a fake-root test
  harness (`tests/fixtures.py`) that runs the installer and uninstaller for
  real against fake `systemctl`, `fuser` and `/proc`, without root or hardware.
- `qnap-tsx70-lcd --print-config KEY` and
  `qnap-tsx70-fancontrol validate-cache`, both read-only.
- `install.sh --allow-serial-owner PID`.

### Testing

The suite is now **330 tests**, up from 234. The new ones execute the failure
paths rather than searching the source for strings: stop failures, PID
identity, dry-run non-mutation proved by hashing the whole fixture tree,
rollback from a failure injected into each phase, a state matrix in which every
artifact starts present and absent, and the documented recovery commands run
against a fixture backup.

**Still not run on physical TS-x70 hardware.** Everything above was validated
offline.

[#2]: https://github.com/marzliak/qnap-ts470-linux/pull/2

## [2.0.0] - 2026-09-12

A breaking release. Everything is renamed, fan control changes behaviour, and
the LCD is now the supported default path. Existing installations are migrated
automatically — see [docs/MIGRATION_FROM_SATURN.md](docs/MIGRATION_FROM_SATURN.md).

> **Not yet run on hardware.** Both binaries are rewrites, validated offline by
> tests and against simulated devices. The hardware behaviour they rely on was
> observed on the reference TS-470 Pro under 1.x, but 2.0.0 itself has not been
> executed on a TS-x70.

### Changed - breaking

- Renamed every binary, unit, cache and visible string from the legacy
  `saturn-*` namespace to `qnap-tsx70-*`. There are **no compatibility
  aliases**; the old names are removed by the migration.
  - `saturn-lcd` becomes `qnap-tsx70-lcd`
  - `saturn-fan-calibrate` becomes `qnap-tsx70-fancontrol`
  - `/etc/saturn-fan-cache.json` becomes `/var/lib/qnap-tsx70/fan-calibration.json`
- Fan control is **no longer installed by default**. Use
  `install.sh --with-fan-control`.
- Fan control now uses explicit subcommands — `status`, `calibrate`, `run` and
  `safe-state` — instead of flags that changed behaviour implicitly.
- Fan calibration is no longer implicit. It requires an explicit command with
  `--yes`, because it deliberately stops the fan.
- The repository name stays `qnap-ts470-linux`.

### Fixed

- **The fan control service never ran its control loop.** The installed unit
  invoked the script without the flag that entered the loop, so the process
  exited cleanly after calibration and `Restart=on-failure` never fired. The
  unit now runs `qnap-tsx70-fancontrol run`, which is a real loop.
- **A lost temperature reading silently became 50 °C.** A missing, empty or
  unparseable sensor now reads as unknown; the loop forces full duty and exits
  nonzero after three consecutive failures so systemd can act.
- **Fan calibration had no signal handling.** `SIGTERM`, `SIGINT`, `SIGHUP`
  (an SSH disconnect) and `SIGQUIT` now all force full duty and restore the
  chip's automatic mode before exiting. Stop signals are blocked for the
  duration of the restore so a second Ctrl-C cannot abandon it half way.
- **A crash could leave a fan parked at its measured stall duty.** Every exit
  path now restores a safe state and reads the register back to confirm it
  took effect. `ExecStopPost=` runs `safe-state` to cover `SIGKILL` and OOM
  kills.
- **A no-fans condition caused a destructive restart loop**, re-probing every
  channel at full duty every ten seconds forever. Failure paths now exit with a
  start limit, so systemd gives up and leaves the chip in charge.
- **Entering disk detail mode with no disks attached crashed the daemon** with
  an `IndexError`. Zero disks is now a supported state, guarded in three
  places.
- **Button frames split across two serial reads were silently dropped.** The
  parser keeps unconsumed bytes between reads and handles partial, multiple
  and interleaved frames.
- **A 16-character line containing a non-ASCII character produced more than 16
  bytes**, desynchronising the fixed-width frame. Text is now encoded before
  truncation, so one source character always costs exactly one column.
- **`smartctl` reporting `"device": null` raised an uncaught `AttributeError`**
  that killed the daemon.
- Hard-coded `hwmonN` fallbacks are gone. Sensors are resolved by driver name,
  re-resolved periodically, and report `N/A` when absent. `hwmon10` no longer
  sorts before `hwmon2`.
- The fan controller path is discovered by driver identity rather than
  hard-coded to one address, and an ambiguous match is refused rather than
  guessed.
- The documented `--calibrate-only` flag was never parsed. Arguments are now
  parsed with `argparse`; an unknown flag is an error, not a silent no-op.
- SMART collection no longer shells out through locale-dependent
  `grep`/`awk` pipelines. It uses `smartctl --json` with timeouts and no shell.
- The installer no longer exits nonzero after a successful install because of a
  trailing `systemctl is-active` under `set -e`.
- **Several display rows silently overflowed 16 columns and were truncated.**
  The clock rendered 17 characters, losing the last digit of the year. The RAM
  row rendered 18 on any machine with 10 GiB or more — including the reference
  unit — losing its unit suffix. The disk summary and the combined power-on
  hours row overflowed at ordinary values. Rows now fit at the widest plausible
  input, verified by a test that renders every page at worst case.
- The manual PID file is gone; systemd tracks the process.

### Added

- `docs/LCD_GUIDE.md` — a complete guide from hardware identification through
  reversible protocol testing, installation, configuration, daily use,
  troubleshooting by symptom, upgrade, rollback and uninstall.
- `docs/LCD_PROTOCOL.md` — the ICP A125 wire protocol, distinguishing verified
  observation from inference.
- `docs/FAN_CONTROL.md` — hazards, safety model, commands and limitations.
- `docs/COMPATIBILITY.md` — per-model, per-feature evidence matrix, and the
  reference machine's full inventory including its CPU swap.
- `docs/MIGRATION_FROM_SATURN.md` — automatic and manual migration, plus
  rollback.
- `scripts/uninstall.sh`, with config and state preserved unless `--purge`.
- `scripts/diagnose.sh`, producing an issue-ready bundle that redacts
  hostnames, IP and MAC addresses, UUIDs and disk serial numbers.
- A configuration file, `/etc/qnap-tsx70-lcd.conf`: serial port, timings,
  sensor names, network interface and page selection.
- `qnap-tsx70-lcd` safe modes: `--check`, `--test-message`, `--once`,
  `--clear` and `--list-pages`.
- `qnap-tsx70-fancontrol safe-state`, to force the fans back to automatic mode.
- A duty floor, so a corrupt or hand-edited calibration cannot pin a fan off.
- Calibration guards: refuses to start above 60 °C, aborts at 70 °C, caps how
  long a fan may sit stopped, discards implausible results, and takes a lock so
  it cannot run against the service.
- Kickstart escalation and a give-up threshold for a fan that will not restart.
- An offline test suite — 234 tests, no hardware required.
- GitHub Actions CI: Python compilation and tests, `bash -n` and ShellCheck,
  `systemd-analyze verify`, and static documentation and privacy checks.
- An MIT `LICENSE`.

### Documentation

- The README no longer presents unverified models as compatible. Only the
  TS-470 Pro is labelled tested; the `Pro` versus non-`Pro` CPU generation
  split is explained, and TS-470U-RP is flagged as unlikely to have an LCD.
- The stock CPU is now cited to QNAP's own archived specification and Intel's
  product page, and is clearly distinguished from the i7-3770T actually fitted
  to the reference machine.
- Removed the unsupported claim that the board is an Intel DQ77MK; that board's
  published specification contradicts the chipset and memory form factor the
  reference unit reports.
- Fan channel 2 is no longer described as universal. The reference unit turns
  channel 3, and the docs now explain that the channel depends on wiring.

### Security

- systemd hardening on both units, with the asymmetry documented: the LCD unit
  sets `ProtectKernelTunables`, the fan unit cannot because it must write to
  `/sys`.
- Both services still run as root, because `smartctl` needs raw device access.
  This is stated plainly rather than presented as privilege minimisation.
- Kernel modules load via `modules-load.d` rather than `ExecStartPre=modprobe`,
  so the units do not need module-loading privileges.
- No shell interpolation of device paths; subprocesses run without a shell and
  with timeouts.

## [1.x] - before 2026-09-12

Initial single-machine implementation under the legacy `saturn-*` namespace:
LCD monitor, fan calibration and control, and an installer. See the git history
for details.
