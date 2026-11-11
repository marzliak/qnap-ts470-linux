# QNAP TS-x70 on Linux — front LCD, sensors and fan control

Use the front-panel LCD of a QNAP TS-x70 NAS after replacing QTS with a normal
Linux distribution. Display system information, read hardware sensors and
browse disk health from the front-panel buttons.

[![CI](https://github.com/marzliak/qnap-ts470-linux/actions/workflows/ci.yml/badge.svg)](https://github.com/marzliak/qnap-ts470-linux/actions/workflows/ci.yml)

## What you get

**LCD monitor** (`qnap-tsx70-lcd`) — installed by default.
Rotates through seven pages on the 2×16 front panel: hostname and IP, CPU
temperature and load, uptime and disk count, CPU utilisation and clock, fan
RPM, network throughput, and board and NIC temperatures. The ENTER and SELECT
buttons open per-disk SMART detail. Three SELECT presses toggle the backlight.

**Fan control** (`qnap-tsx70-fancontrol`) — **experimental, opt-in, not
installed by default.**
Drives the chassis fan from CPU temperature. Its calibration step deliberately
stops the fan to find its limits. You do not need it to use the LCD. Read
[docs/FAN_CONTROL.md](docs/FAN_CONTROL.md) first. The
[official-firmware findings](docs/FAN_FIRMWARE_FINDINGS.md) identify QTS's
Fintek control route; physical acceptance of this package remains pending.

## Compatibility and validation status

| Scope | Status |
|---|---|
| Reference hardware | QNAP **TS-470 Pro**; hardware behaviour observed with the earlier implementation on 2026-09-12 |
| Latest host snapshot — 2026-11-06 | **Ubuntu 24.04.5 LTS**, kernel **7.0.0-42-generic**, x86_64; running OS/kernel checked on the reference unit |
| Current implementation | Offline tests and simulated devices only; **not yet validated on physical TS-x70 hardware** |
| Other tower models | TS-670 Pro, TS-870 Pro, TS-470 / TS-670 / TS-870 (non-Pro): **unverified** |
| TS-470U-RP | LCD support **unlikely** — QNAP's product page does not list an LCD |

Hardware observations are not acceptance tests of the current software. The
LCD, installer, migration, rollback and diagnostics still need validation on a
physical unit; fan control remains experimental.

`Pro` and non-`Pro` models may differ in CPU generation, board and controller.
See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for the evidence and the
reference configuration, including the dated
[software snapshot](docs/COMPATIBILITY.md#latest-software-snapshot).

Read [docs/LCD_GUIDE.md](docs/LCD_GUIDE.md) before installing. The LCD writes to
a serial port as root; optional fan control writes to the fan controller.

## Quick start (LCD only)

```bash
sudo apt install -y python3 smartmontools util-linux
git clone https://github.com/marzliak/qnap-ts470-linux.git
cd qnap-ts470-linux
```

Check your hardware before changing anything — this writes nothing:

```bash
sudo ./bin/qnap-tsx70-lcd --check
```

Prove the panel actually responds, reversibly:

```bash
sudo ./bin/qnap-tsx70-lcd --test-message "qnap-tsx70|hello"
sudo ./bin/qnap-tsx70-lcd --clear
```

If text appeared, install:

```bash
sudo ./scripts/install.sh
```

That installs the LCD service only, enables it at boot, and starts it. Verify:

```bash
systemctl status qnap-tsx70-lcd
journalctl -u qnap-tsx70-lcd -f
```

The full walkthrough, including what to do when nothing appears, is in
[docs/LCD_GUIDE.md](docs/LCD_GUIDE.md).

### Adding fan control later

```bash
sudo ./scripts/install.sh --with-fan-control
```

On a first install, the fan service is left stopped and is not enabled without
a valid calibration. Calibration deliberately stops the fan. Calibrate first,
then use `sudo systemctl enable --now qnap-tsx70-fancontrol`. An upgrade can
restart a previously running, calibrated fan service. See
[docs/FAN_CONTROL.md](docs/FAN_CONTROL.md).

### Removing it

```bash
sudo ./scripts/uninstall.sh            # keeps your config and calibration
sudo ./scripts/uninstall.sh --purge    # removes those too
```

## The reference hardware

The hardware observations come from a **modified TS-470 Pro**: an Intel Core
i7-3770T replaces the stock Core i3-3220, 16 GiB of DDR3-1600 SO-DIMM is fitted,
Linux boots from a SATA SSD, and an add-in 2.5 GbE NIC complements the two stock
Intel gigabit ports.

These upgrades are not required by this project. The CPU swap is not a
recommendation: check CPU power requirements against the chassis cooling
before considering a replacement.

The recorded inventory, stock specifications and sources are in
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md#3-the-tested-reference-machine).

## Documentation

| Document | What it covers |
|---|---|
| [LCD guide](docs/LCD_GUIDE.md) | Hardware checks, installation, configuration, daily use, troubleshooting and removal |
| [LCD protocol](docs/LCD_PROTOCOL.md) | ICP A125 commands, framing, timing, buttons and evidence limits |
| [Fan control](docs/FAN_CONTROL.md) | Experimental feature: hazards, safety model, calibration and limitations |
| [Compatibility](docs/COMPATIBILITY.md) | Per-model evidence, reference hardware and compatibility reports |
| [Legacy migration](docs/MIGRATION_FROM_SATURN.md) | For existing installations using the old `saturn-*` layout; backup, migration and rollback |
| [Changelog](CHANGELOG.md) | Version history and user-visible changes |

## Repository layout

```text
bin/        qnap-tsx70-lcd, qnap-tsx70-fancontrol
config/     example configuration
systemd/    unit files — the installer copies these, it does not generate them
scripts/    install.sh, uninstall.sh, diagnose.sh
docs/       guides and references
tests/      offline test suite, no hardware required
```

## Requirements

- A QNAP TS-x70 with the ICP A125-compatible front panel
- Linux with systemd, and Python 3.9 or newer (standard library only)
- `smartmontools` and `util-linux` for disk pages — optional, the display
  degrades to `N/A` without them
- root, for the serial port and for SMART

## Reporting a problem

```bash
sudo ./scripts/diagnose.sh -o bundle.txt
```

The bundle redacts hostnames, IP and MAC addresses, UUIDs and disk serial
numbers, but **review it before posting**. Redaction is not a guarantee that
all identifying information has been removed.

Reports testing the current software on a TS-470 Pro or another model are
welcome, including negative results. See
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md#5-contributing-a-report).

## Acknowledgements

The protocol work builds on community projects that reverse-engineered QNAP
front panels and embedded controllers. They are credited in
[docs/LCD_PROTOCOL.md](docs/LCD_PROTOCOL.md#7-references).

## License

[MIT](LICENSE).
