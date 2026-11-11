# Changelog

Notable user-facing changes are documented here, grouped by version.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

The current implementation has been validated offline with simulated devices
and a fake filesystem. It has not yet been validated on physical TS-x70
hardware. See [compatibility](docs/COMPATIBILITY.md).

### Added

- `scripts/uninstall.sh --fan-only` removes fan control while preserving the
  LCD service and its configuration. The same fan-stop and automatic-mode
  checks apply as for a full uninstall.
- `qnap-tsx70-lcd --print-config KEY` returns an effective configuration value;
  `qnap-tsx70-fancontrol validate-cache` checks a calibration file. Both are
  read-only.
- `install.sh --allow-serial-owner PID` explicitly acknowledges an otherwise
  unrecognised serial-port owner. It requires `fuser` or `lsof`, does not
  signal the named process, and does not bypass the port-release check.

### Fixed — installation, migration and removal

- Migration and reinstall recognise the existing LCD service as a planned
  serial-port owner. The service is stopped after backup, and the port must be
  released before the replacement starts. Unknown owners still block the
  install; `--force` does not bypass this check.
- Install and uninstall abort if an affected fan service fails to stop or a
  fan-control writer remains. Legacy PID files are checked against process
  identity before signalling; an unresponsive process is not force-killed.
- LCD-only operations leave unrelated fan services and artifacts untouched.
  `--no-migrate` preserves legacy services and reports a conflicting legacy
  LCD owner before making changes.
- A first fan-control install is not enabled without valid calibration.
  Reinstall restores a previously active fan service when calibration permits,
  and the summary reports its actual state.
- Lifecycle operations hold the fan writer's canonical lock while replacing,
  removing or restoring fan artifacts. Changing the calibration-file path
  cannot create an independent writer lock.
- Install, uninstall and rollback use the repository's fan verifier and require
  confirmed automatic mode before changing fan recovery artifacts. A missing,
  unreadable or unrunnable verifier blocks removal.
- Rollback restores the recorded contents, permissions and unit states of
  artifacts actually changed by the operation. Fan-related rollback repeats
  stop, writer and automatic-mode checks instead of reusing an earlier result.
  If those checks fail, fan recovery artifacts are preserved, skipped paths
  are reported, and rollback exits nonzero.
- Dry runs do not mutate the host, including when an error triggers rollback.
- Invalid or superseded legacy calibration files are preserved. Manual
  migration deletes a legacy cache only when that exact file was migrated.
- Shell scripts read effective configuration through the runtime parser,
  avoiding differences in quote and inline-comment handling.

### Fixed — fan control

- Recovery no longer attempts duty writes on channels already in automatic
  mode, where the real driver returns `EROFS`. Manual channels retain the
  full-duty attempt before automatic handback.
- Duty acknowledgement accepts the exact documented RPM-target round trip
  with an unchanged valid full-speed scale, not an arbitrary tolerance.
- Recovery logs distinguish driver acknowledgement from physical effect;
  cached sysfs values do not independently verify fan actuation.
- Read-only status preserves unknown RPM and includes `full_speed_rpm`.
- Manual-mode and duty writes require documented driver readback. Calibration
  aborts on a refused write, unreadable register or mismatched value before
  advancing the sweep.
- Consecutive write failures are tracked independently per channel and
  register, so a healthy channel cannot hide another channel's failures.
- Calibration aborts immediately on missing, malformed, non-finite or
  implausible temperature readings.
- Calibration probes run inside the verified cleanup scope, including early
  exits and channels touched by a probe but not classified as active.
- `safe-state` succeeds only when every target channel's writes and automatic
  mode are confirmed. Missing controllers or controllable channels are
  failures, not successful handbacks.
- `run` and `calibrate` report failed cleanup as an error. A calibration with
  unconfirmed cleanup does not save its results or recommend starting the
  service. Repeated stop signals cannot turn a failed restoration into success
  or abandon the remaining channels.
- `safe-state --dry-run` exits **3** and reports that no hardware state was
  verified. Only a verified handback exits 0.

### Fixed — diagnostics

- Diagnostic redaction covers IPv6 compressed, expanded, CIDR, zone-ID and
  IPv4-mapped forms without treating ordinary timestamps as addresses.
- Diagnostic file output is written atomically with mode 0600. Collection,
  redaction and destination errors return nonzero rather than reporting a
  successful bundle.

### Documentation

- Added exact-model official-QTS firmware findings for issue #1: Fintek
  F71869A/SIO route, channel 2 mapping, the contradictory later channel 3
  observation, Linux ABI limits and the remaining physical acceptance gate.
- Updated the reference host's OS/kernel snapshot to Ubuntu 24.04.5 LTS and
  7.0.0-42-generic, observed on 2026-11-06. Historical hardware measurements
  remain dated separately; the current package's validation status is unchanged.
- The README leads with capabilities and installation. Legacy migration
  remains linked as a guide for existing users rather than a standalone
  feature section.
- README and guides distinguish earlier reference-hardware observations from
  offline validation of the current implementation; neither implies current
  hardware acceptance or established stability.
- Manual migration and rollback check stopped services, surviving writers,
  serial-port ownership and automatic fan mode before removing artifacts.
  Rollback restores files to their respective binary, unit and state paths.
- The fan-only removal guide uses `uninstall.sh --fan-only` rather than manual
  deletion of recovery artifacts.

### Testing

- Offline regression tests exercise lifecycle failure paths, calibration and
  register failures, signal handling, writer locking, redaction, and the
  documented migration, rollback and fan-only uninstall procedures against
  simulated devices and a fake root. These tests do not establish physical
  hardware acceptance.

## [2.0.0] - 2026-09-12

LCD monitoring becomes the default installation; fan control is experimental
and opt-in. This version was validated offline, not on physical TS-x70
hardware. The reference-hardware observations were made with the earlier
implementation.

### Changed — breaking

- Binaries, services and state paths move from `saturn-*` to `qnap-tsx70-*`,
  without compatibility aliases. Examples: `saturn-lcd` becomes
  `qnap-tsx70-lcd`, `saturn-fan-calibrate` becomes `qnap-tsx70-fancontrol`,
  and `/etc/saturn-fan-cache.json` moves to
  `/var/lib/qnap-tsx70/fan-calibration.json`. Existing installations should
  follow the [migration guide](docs/MIGRATION_FROM_SATURN.md).
- Fan control is no longer installed by default; use
  `install.sh --with-fan-control` to include it.
- Fan control uses explicit `status`, `calibrate`, `run` and `safe-state`
  subcommands. Calibration requires explicit confirmation with `--yes`
  because it deliberately stops the fan.

### Added

- LCD configuration in `/etc/qnap-tsx70-lcd.conf`: serial port, timing, sensor
  names, network interface and page selection. LCD commands include `--check`,
  `--test-message`, `--once`, `--clear` and `--list-pages`.
- Installer, uninstaller and redacted diagnostic-bundle tooling. Configuration
  and state are retained on uninstall unless `--purge` is requested.
- Fan calibration limits: refuse to start above 60 °C, abort at 70 °C, bound
  stall duration and reject implausible results. Normal operation has a duty
  floor, restart escalation and a give-up threshold.
- Operator guides for LCD use, protocol, fan control, compatibility, migration
  and rollback, plus an offline test suite and GitHub Actions CI.
- MIT license.

### Fixed

- The fan service starts the control loop rather than exiting after
  calibration. Missing temperature readings are treated as unknown rather
  than replaced with a nominal 50 °C value.
- Fan-stop handling attempts full duty and automatic-mode restoration;
  `ExecStopPost=` provides a recovery attempt after abrupt termination.
  Service start limits prevent repeated probing when no fan is found. See
  Unreleased for subsequent verification and cleanup corrections.
- LCD operation handles zero disks, fragmented and interleaved button frames,
  non-ASCII text, and null SMART device fields without crashing or overflowing
  the fixed-width display. Clock, RAM and disk-detail rows fit 16 columns.
- Sensors and controllers are discovered by driver identity rather than fixed
  `hwmonN` indices or controller addresses. Ambiguous controllers are refused;
  missing sensor readings display `N/A`.
- SMART collection uses timed JSON subprocesses without shell pipelines.
  Unknown CLI options are errors, and systemd replaces the manual PID file.
- Successful installation no longer exits with an error because of a trailing
  service-state check.

### Security

- Both units include systemd hardening. The LCD unit protects kernel tunables;
  the fan unit must retain sysfs write access. Both services run as root.
- Kernel modules load through `modules-load.d`; subprocesses use timeouts
  without shell interpolation of device paths.

### Documentation

- Compatibility separates the observed TS-470 Pro from unverified models,
  distinguishes Pro and non-Pro variants, and records the modified reference
  configuration separately from stock specifications and requirements.
- Fan channels are described as wiring-dependent rather than universal.

## [1.x] - before 2026-09-12

Initial single-machine LCD monitor, fan calibration and control, and installer
using the legacy `saturn-*` namespace. Details remain in the Git history.
