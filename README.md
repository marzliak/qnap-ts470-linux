# QNAP TS-x70 on Linux — front LCD, sensors and fan control

Bring the front-panel LCD of a QNAP TS-x70 NAS back to life after replacing QTS
with a normal Linux distribution, and read the hardware sensors while you are
at it.

The hardware is still good. This project keeps the parts of it that stop
working the moment you install your own OS.

[![CI](https://github.com/marzliak/qnap-ts470-linux/actions/workflows/ci.yml/badge.svg)](https://github.com/marzliak/qnap-ts470-linux/actions/workflows/ci.yml)

---

## ⚠️ Tested on exactly one machine

| | |
|---|---|
| **Tested** | QNAP **TS-470 Pro**, Ubuntu 24.04 LTS, kernel 7.0 series |
| **Unverified** | TS-670 Pro, TS-870 Pro, TS-470 / TS-670 / TS-870 (non-Pro) |
| **Unlikely** | TS-470U-RP — QNAP's own product page does not list an LCD |

Everything outside the first row is an estimate from published specifications,
not a test result. `Pro` is not a cosmetic suffix: the Pro towers are Ivy
Bridge, the non-Pro models of the same number are an older Sandy Bridge
platform. See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

This software writes to a serial port and, optionally, to your fan controller.
Read [docs/LCD_GUIDE.md](docs/LCD_GUIDE.md) before installing.

---

## What you get

**LCD monitor** (`qnap-tsx70-lcd`) — the stable part.
Rotates through seven pages on the 2×16 front panel: hostname and IP, CPU
temperature and load, uptime and disk count, CPU utilisation and clock, fan
RPM, network throughput, and board and NIC temperatures. The ENTER and SELECT
buttons open per-disk SMART detail. Three SELECT presses toggle the backlight.

**Fan control** (`qnap-tsx70-fancontrol`) — **experimental, opt-in, not
installed by default.**
Drives the chassis fan from CPU temperature. Its calibration step deliberately
stops the fan to find its limits. You do not need it to use the LCD. Read
[docs/FAN_CONTROL.md](docs/FAN_CONTROL.md) first.

---

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

This installs and enables the service but does **not** start it, because it
needs a calibration run that stops your fan. See
[docs/FAN_CONTROL.md](docs/FAN_CONTROL.md).

### Removing it

```bash
sudo ./scripts/uninstall.sh            # keeps your config and calibration
sudo ./scripts/uninstall.sh --purge    # removes those too
```

---

## Upgrading from the old `saturn-*` names

Version 2.0.0 renamed every binary, unit and path. The installer detects a
legacy installation, backs it up, migrates it and rolls back if anything fails.
Run `sudo ./scripts/install.sh` and read
[docs/MIGRATION_FROM_SATURN.md](docs/MIGRATION_FROM_SATURN.md).

---

## The reference hardware

The machine this is tested on is a TS-470 Pro that has been **modified**: it
runs an Intel Core i7-3770T in place of the stock Core i3-3220, has 16 GiB of
DDR3-1600 SO-DIMM fitted, boots from a SATA SSD, and carries an add-in 2.5 GbE
NIC alongside the two stock Intel gigabit ports.

The CPU swap is not required by anything here, and it is not a recommendation.
The `T` part is a 45 W chip, below the stock 55 W, which is the right direction
for a chassis built around a single 9 cm fan — fitting something hotter is a
thermal problem, not an upgrade.

The full inventory, the stock specification it is being compared against, and
the sources for both are in
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md#3-the-tested-reference-machine).

---

## Documentation

| Document | What it covers |
|---|---|
| [LCD_GUIDE.md](docs/LCD_GUIDE.md) | Complete install-to-uninstall guide: preflight, reversible protocol test, install, configuration, daily use, systemd, troubleshooting by symptom, rollback |
| [LCD_PROTOCOL.md](docs/LCD_PROTOCOL.md) | ICP A125 wire protocol: command bytes, framing, timing, button frames, verified vs inferred |
| [FAN_CONTROL.md](docs/FAN_CONTROL.md) | Experimental fan control: hazards, safety model, commands, limitations |
| [COMPATIBILITY.md](docs/COMPATIBILITY.md) | Per-model and per-feature evidence, reference hardware, how to contribute a report |
| [MIGRATION_FROM_SATURN.md](docs/MIGRATION_FROM_SATURN.md) | Upgrading from the legacy `saturn-*` layout |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

---

## Repository layout

```text
bin/        qnap-tsx70-lcd, qnap-tsx70-fancontrol
config/     example configuration
systemd/    unit files — the installer copies these, it does not generate them
scripts/    install.sh, uninstall.sh, diagnose.sh
docs/       guides and references
tests/      offline test suite, no hardware required
```

---

## Requirements

- A QNAP TS-x70 with the ICP A125-compatible front panel
- Linux with systemd, and Python 3.8 or newer (standard library only)
- `smartmontools` and `util-linux` for disk pages — optional, the display
  degrades to `N/A` without them
- root, for the serial port and for SMART

---

## Reporting a problem

```bash
sudo ./scripts/diagnose.sh -o bundle.txt
```

The bundle is redacted — hostnames, IP and MAC addresses, UUIDs and disk serial
numbers are replaced with placeholders — but read it before posting it.

Reports from models other than the TS-470 Pro are especially welcome, including
negative ones. See
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md#5-contributing-a-report).

---

## Acknowledgements

The protocol work builds on several community projects that reverse-engineered
QNAP front panels and embedded controllers; they are credited with links in
[docs/LCD_PROTOCOL.md](docs/LCD_PROTOCOL.md#7-references).

## License

[MIT](LICENSE).
