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

### Fixed — second review pass on the same paths

A follow-up review of the fixes above found five more failure paths. Every one
of them is a case where a guard existed but did not hold, or held something it
had no business holding.

- **A fan writer is identified by the command it is running, not by a word in
  its command line.** `install.sh` and `uninstall.sh` classified a process
  read-only when *any* argv token equalled `status`, `validate-cache`,
  `--version` or `--help`, so `qnap-tsx70-fancontrol run --sensor status` — a
  live control loop — passed the quiescence gate and its binary and unit were
  removed underneath it. Both scripts now parse argv structurally: find the
  executable, walk past options and their values, and read the command word
  that `bin/qnap-tsx70-fancontrol`'s own parser requires before any
  per-command option. `run`, `calibrate` and `safe-state` are writers whatever
  follows them; `status` and `validate-cache` are still non-blocking; and
  anything unclassifiable — an unknown command, no command at all, an argv
  without the executable in it — counts as a writer.
- **The manual migration and the manual rollback are fail-closed.** Both
  blocks in `docs/MIGRATION_FROM_SATURN.md` stopped services with
  `2>/dev/null || true`, printed `is-active` as a report rather than a gate,
  and then deleted the binaries and units regardless. A reader pasting them on
  a host whose fan service would not stop destroyed exactly what the automatic
  path refuses to touch. Both are now single self-contained scripts whose
  first step defines the guards every later step calls: a failed stop, a stop
  that leaves the unit up, a unit still activating, a surviving writer, a live
  PID file naming something unidentified, and a serial port still held each
  abort before the first `rm`. The deletion step re-checks rather than
  trusting the earlier one, because the new service is started in between.
- **Only what a run replaces or removes is stopped.** An LCD-only install
  writes no fan binary, so stopping a running `qnap-tsx70-fancontrol.service`
  was collateral damage — the machine came back with fan control down. A
  `--no-migrate` install stopped and disabled the legacy fan and LCD services
  it had promised to leave alone. The installer now plans its scope before
  preflight and every stop and quiescence gate reads it. A fan service that
  *was* in scope and active is started again once its replacement is in place,
  or reported when there is no valid calibration to start it with. The one
  case `--no-migrate` cannot have both ways — the legacy LCD holding the
  serial port the new service needs — is now reported during preflight,
  before anything is changed, instead of being called "the planned
  transition" and discovered after the install.
- **The manual cache deletion keys off provenance.** Step 9 deleted
  `/etc/saturn-fan-cache.json` whenever a valid calibration existed at the new
  path — including when that calibration was already there and step 6 had
  deliberately declined to migrate over it. The block now records whether that
  step migrated that exact file, and deletes it only on that evidence.
- **An unreadable serial port is no longer something a flag can vouch for.**
  With neither `fuser` nor `lsof` installed, `--allow-serial-owner PID` could
  not check that the PID held the port, that it existed, or that the port was
  free before the service opened it — and then printed that it had. The
  installer now fails closed and names the package to install; `verify_port_
  released` has no bypass either. `--force` remains unrelated to both, and
  `--allow-serial-owner` still works where the port can actually be read.

### Fixed — third review pass: the fan safe state was never verified

A third review looked only at the fan path and found that the mechanism the
whole design rests on — "force full duty, hand the channels back to the chip,
then read the register back to confirm" — did not actually confirm anything.
Everything below is offline work: none of it has been run on a TS-x70.

- **The safe state is now a verdict, not an attempt.** `safe_state()` returns
  true only when every target channel reads back `pwmN_enable = 2`. It used to
  ignore the result of both writes and accept an unreadable register next to
  automatic mode (`if mode not in (None, PWM_AUTOMATIC)`), so the one channel
  most likely to be parked at a stall duty — one whose registers had stopped
  answering — was the one logged as `-> automatic mode`. A failed write now
  fails the channel too: the readback comes from the same register interface
  that just refused a write, and is not evidence on its own. Every discovered
  channel is still attempted even after an earlier one fails, and the
  aggregate is reported per channel.
- **`safe-state` exits nonzero when it cannot prove the handback**, including
  when the chip exposes no controllable channel at all. `scripts/uninstall.sh`
  and any operator script can now use that exit status as a gate.
- **`run` and `calibrate` surface a failed restoration.** A `SIGTERM` stop that
  could not confirm automatic mode exits nonzero instead of reporting a clean
  shutdown, and a calibration whose cleanup could not be confirmed exits
  nonzero and does **not** save its results or print "start the service when
  you are ready".
- **Calibration aborts when the temperature stops arriving.** The sweep
  deliberately lowers the duty until the fan stops, and it validated the
  temperature only when one happened to be readable — a sensor that vanished
  mid-sweep left it stepping the fan down blind. Every settle sample is now
  validated against one documented plausible band shared with the control
  loop (`TEMP_PLAUSIBLE_MIN`/`TEMP_PLAUSIBLE_MAX`); a missing, unparseable,
  NaN, infinite or out-of-band reading aborts immediately, before the next
  step down, and the cleanup still runs.
- **The uninstaller gates every removal on the safe state.** It used to run
  `safe-state`, ignore the answer, and delete the binary and the unit anyway —
  removing the one executable that could have fixed what it had just been told
  about. An unverified safe state now stops the run before anything is
  removed, including before the LCD service is touched, keeps the fan binary
  and its unit in place as the recovery mechanism, and prints how to recover.
  A fan binary that exists but cannot be executed is the same refusal, because
  the removal step would have deleted it regardless.
- **The fan-only uninstall recipe in `docs/FAN_CONTROL.md` is gone.** It was
  `systemctl disable --now` followed by two unconditional `rm`s: `disable
  --now` reports success for a unit that is still shutting down, says nothing
  about a writer that outlived its unit, and cannot tell anyone whether the
  chip took the fans back. The guide now points at
  `sudo ./scripts/uninstall.sh --fan-only`, and the tests run whatever command
  the guide gives against a fake root rather than reading it.

### Fixed — fourth review pass: the fan writer and the handback blockers

A fourth review took the fan path apart again, this time looking at what
*writes* to the chip rather than at what reads back from it. Everything below
is offline work: none of it has been run on a TS-x70.

- **Calibration checks every control write.** `calibrate_channel()` called
  `set_manual()` and `set_pwm()` and threw both answers away, so a chip that
  never left its own automatic curve, or that stopped acknowledging duties
  half way down the sweep, still produced a full set of measurements, a saved
  cache and an exit status of 0 — describing a fan whose real duty nobody
  knew. Every control write is now a hard gate: manual mode, the spin-up, each
  step of the descent, the zero duty, each step of the restart search, the
  measured minimum and the cool-down. The first refusal aborts with a
  `FanError` naming the phase and the duty, because the next step of a sweep
  is always *lower* and taking it would be stepping a fan towards its stall
  point blind. Nothing is analysed and nothing is saved from a sweep that was
  refused; the aggregate safe state is still attempted, and a cleanup that
  also fails is reported rather than hidden behind the abort. A `--probe` the
  chip does not accept no longer marks a channel active.
- **Write failures are counted per channel and per register.** The control
  loop kept one counter for the whole chip and reset it on any successful
  write, so on a two-fan board the healthy fan zeroed the dead one's history
  every iteration: a PWM register that refused every command for the life of
  the service never reached `WRITE_FAILURE_LIMIT`. `FanController` now keeps
  `{(channel, register): consecutive failures}`, a success clears only its own
  entry, and any single register reaching the limit fails the whole service
  safe. The initial `set_manual()` result is also checked before the loop is
  entered — every channel is attempted first, so the safe state still has all
  of them — because a channel still on the chip's own curve is not one this
  program controls.
- **One writer lock per physical controller, not one per cache file.** The
  lock was placed beside whatever `--cache` named, so `systemctl start
  qnap-tsx70-fancontrol` and `calibrate --cache /tmp/c` were two writers on
  the same PWM registers, each holding a lock the other never looked at —
  while the sweep was deliberately stalling a fan. Writers now take
  `/run/qnap-tsx70/fancontrol.lock` and then
  `/run/qnap-tsx70/fancontrol-<controller>.lock`, in that order; the identity
  is the platform device's own directory name, sanitised. `--cache` cannot
  move either. The first is what a writer that cannot resolve a controller
  takes on its own, so the unresolved case serialises against the resolved one
  instead of racing it. `safe-state` still takes no lock, deliberately and
  explicitly: a recovery that the thing it is recovering from can block is not
  a recovery.
- **The installer proves the handback before deleting a fan recovery
  artifact.** It deleted the legacy `saturn-*` fan binaries and units, and
  replaced the current ones under `--with-fan-control`, on the strength of an
  inactive unit and an empty process table — both of which are equally true of
  a machine whose fan is parked at a calibration stall duty with
  `pwmN_enable` still reading 1. Once the in-scope fan writers are proven
  stopped, and immediately before the first destructive step, it now runs the
  repository copy of `qnap-tsx70-fancontrol safe-state` against the controller
  and continues only on exit 0. A refusal keeps every fan binary and unit
  where it is and rolls the run back. Scope is enforced: an install that
  replaces or deletes no fan artifact — the ordinary LCD-only one, a migration
  of a display-only legacy tree, a first `--with-fan-control` install — never
  writes to a fan controller at all. This is not delegated to the unit's
  `ExecStopPost=`, whose `-` prefix tells systemd to ignore its exit status.
- **The documented manual migration and rollback carry the same gate.** Both
  blocks in `docs/MIGRATION_FROM_SATURN.md` now call the repository binary's
  `safe-state` after their quiescence checks and before their first `rm` of a
  fan binary or unit, and abort with recovery instructions if it does not exit
  0. The tests execute both blocks against a fake chip, so the gate cannot be
  documented and absent.
- **`safe-state --dry-run` no longer claims a verified handback.** It wrote no
  register, read none back, printed "automatic fan mode verified on channels
  …" and exited 0 — so every caller that gates a deletion on the exit status
  could have been handed the go-ahead by a flag guaranteeing nothing happened.
  A dry run now prints what it would write, says plainly that nothing was
  proven, and exits **3**: distinct from 0 (proven back) and from 1 (proven
  not back), so a caller treating anything but 0 as "do not delete" is right
  either way.

### Fixed — fifth review pass: the signal, the probe and the rollback

Three more paths where a guard was in place and something walked around it.

- **A second stop signal can no longer turn a failed handback into exit 0.**
  `safe_state()` blocks `SIGTERM`, `SIGINT`, `SIGHUP` and `SIGQUIT` while it
  puts the channels back, so the kernel holds any that arrive until the mask
  comes down — and it restored that mask in a `finally` placed in front of the
  aggregate verdict. A signal delivered by the unmasking raised `Terminated`
  from inside `signal.pthread_sigmask()` itself, unwound past the `return
  False` underneath it, and reached `main()`, which reports a signalled stop as
  exit 0. A `run` stopping with a fan still in manual mode, a `calibrate` whose
  cleanup had just failed, and the `safe-state` that `install.sh` and
  `uninstall.sh` gate every deletion of a fan binary on all exited 0 that way.
  The verdict is now computed, logged and recorded while the signals are still
  blocked; the unmasking drains them and hands the first one back through
  `deferred_stop` instead of raising it; and the commands re-raise it only once
  the exit status is settled, so a proven handback still ends as a signalled
  stop and an unproven one cannot. A failed verdict is also recorded in the
  process rather than only returned, so a `Terminated` arriving at any later
  point cannot walk past it: `main()` consults that record before believing any
  signal. A signal that fires *inside* the restore loop — one that arrived just
  before the mask went up — no longer abandons the channels after it; the
  interrupted channel is retried once, twice at most, and the rest are still
  attempted.
- **Calibration probes inside the verified cleanup scope.** `--probe` puts a
  silent channel into manual mode at full duty to find out whether a fan is
  attached, and it ran in front of the `try`/`finally` whose aggregate
  `safe_state()` is what every calibration exit status is built on. The only
  thing behind it was `_Borrowed.restore()`, which wrote the old values back
  and checked nothing. A chip that accepted the probe and then refused
  automatic mode therefore left calibration through one of two doors — "no
  controllable fan channel is spinning" and "the cached calibration already
  matches" — with a channel in manual mode, no aggregate handback, and, on the
  second door, exit 0. Everything that can write a register now happens inside
  that scope, the cleanup covers every channel the probe touched even when it
  was not classified active, `restore()` reports whether it landed, and a
  probe that cannot be undone exits nonzero, saves no cache and recommends no
  service start. A run that wrote no register at all — `--channels` with a
  matching cache — is distinguished from a safe state with no target, which is
  still a failure.
- **Rollback proves the handback again instead of reusing a stale verdict.**
  The pre-install verdict is earned before `install_files()` replaces the fan
  binary and before `enable_services()` calls `restore_fan_activity()`, which
  starts a fan service that was running before the install back up. By the time
  `rollback()` restores or removes a fan binary or unit, that service is a live
  writer again and the old verdict describes a machine that no longer exists.
  The rollback now decides from the transaction manifest whether it would
  change a fan binary or unit at all; if it would, it clears the old verdict,
  stops every current and legacy fan unit that may be running, proves no fan
  writer of any name survived, and runs the repository `safe-state` again.
  Only on exit 0 may it touch those paths. If any step fails it changes no fan
  recovery artifact, still rolls back everything else, names the paths it left
  alone both on the console and in `txn/fan-rollback-skipped`, does not start
  the fan service again onto files it never restored, and exits nonzero. A
  transaction with no fan artifact in it — the ordinary LCD-only install —
  reaches no gate and writes to no controller, which is the same scope rule the
  install-time gate follows.

### Added

- `scripts/uninstall.sh --fan-only`, which removes the fan binary, its unit
  and the modules file and leaves the LCD service, its unit and its
  configuration untouched. It runs the same stop, quiescence and safe-state
  gates as a full uninstall.
- `scripts/redact.py`, the diagnostic redaction filter, and a fake-root test
  harness (`tests/fixtures.py`) that runs the installer and uninstaller for
  real against fake `systemctl`, `fuser` and `/proc`, without root or hardware.
- `qnap-tsx70-lcd --print-config KEY` and
  `qnap-tsx70-fancontrol validate-cache`, both read-only.
- `install.sh --allow-serial-owner PID`, for a port held by a process the
  installer does not recognise. It requires `fuser` or `lsof`: it makes no
  claim on a host where the port cannot be read at all.

### Testing

The suite is now **563 tests**, up from 234. The new ones execute the
failure paths rather than searching the source for strings: stop failures, PID
identity, fan-command classification against `run --sensor status` and its
relatives, scope assertions for an active fan service during an LCD-only
install and for active legacy services under `--no-migrate`, dry-run
non-mutation proved by hashing the whole fixture tree, rollback from a failure
injected into each phase, a state matrix in which every artifact starts present
and absent, and the guide's manual migration and rollback blocks executed
end to end against a fake root — with failed stops, ineffective stops and
lingering writers — rather than read.

The third pass added 55 of those: a chip that takes a write to `pwmN_enable`
and stays in manual mode, a register that reads back as nothing, a write the
chip refuses, a chip with no controllable channel, one bad channel out of
three, a `SIGTERM` stop whose restore cannot be confirmed, a calibration whose
sensor disappears mid-sweep and one whose cleanup fails, and the uninstaller
and the guide's own fan-only command run against that same fake chip. The
uninstaller tests execute the real `bin/qnap-tsx70-fancontrol` rather than a
stub that agrees with them, so what the gate trusts is what `safe-state`
actually verifies.

The fourth pass added 76 more, in two new files and four existing ones: every
calibration control write refused one at a time and the sweep proved to stop
rather than step lower; a two-channel control loop in which one PWM register
refuses every command while the other answers every one; a refused
manual-mode command before the loop starts; two real processes contending for
the same controller's writer lock from different `--cache` directories, and
`safe-state` proved to still work while one of them holds it; the installer
and both documented procedures run against a fake chip that stays in manual
mode, refuses the write, reads back nothing, exposes no controllable channel
or is not there at all, each asserting that no fan binary or unit was deleted;
and a dry run proved to change nothing and to be unusable as a handback.

The fifth pass added 36 more, in three new files: two stop signals raised at
this process for real and delivered by the unmasking at the end of a safe
state, over a total restoration failure, a partial one and a successful one,
for `safe-state`, `run` and `calibrate`; a signal raised inside the restore
loop, proved to be retried and not to abandon the channels after it; a
`--probe` the chip accepts and will not undo, one that finds no fan at all, one
that is refused outright, a partial two-channel probe, and the up-to-date-cache
exit that used to skip the cleanup entirely; and an installer rollback in which
fan control was restarted before it, its stop fails, its stop does not take, a
writer survives it, the chip stays in manual mode, refuses the write, reads
back nothing or is not there — each asserting the *contents* of the fan binary
and unit file and the state of the unit, not only the command log.

Every safety guard added or changed here was mutation-tested: the production
guard was removed or weakened one at a time and the run repeated to confirm
that the test protecting it fails, and fails on its own assertion rather than
on an error. 32 mutations were applied across the fourth pass's guards and all
32 were caught; 22 across the fifth pass's, all 22 caught. No safety claim
rests on a test that only greps a source file.

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
