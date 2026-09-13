# LCD guide

Everything needed to get the front-panel display of a QNAP TS-x70 working under
Linux, from checking your hardware to removing the software again.

Written for someone who has never seen this machine before. If you have used a
terminal and can run `sudo`, you have enough background.

---

## Contents

1. [What this does and does not do](#1-what-this-does-and-does-not-do)
2. [Safety](#2-safety)
3. [Prerequisites](#3-prerequisites)
4. [Identifying your hardware](#4-identifying-your-hardware)
5. [Testing the panel before installing anything](#5-testing-the-panel-before-installing-anything)
6. [Installing](#6-installing)
7. [Verifying](#7-verifying)
8. [Configuration](#8-configuration)
9. [Daily use: pages and buttons](#9-daily-use-pages-and-buttons)
10. [Running it under systemd](#10-running-it-under-systemd)
11. [Customising the pages](#11-customising-the-pages)
12. [Protocol limits worth knowing](#12-protocol-limits-worth-knowing)
13. [Troubleshooting by symptom](#13-troubleshooting-by-symptom)
14. [Upgrading](#14-upgrading)
15. [Rolling back](#15-rolling-back)
16. [Uninstalling](#16-uninstalling)
17. [Collecting a diagnostic bundle](#17-collecting-a-diagnostic-bundle)
18. [Contributing a compatibility report](#18-contributing-a-compatibility-report)

---

## 1. What this does and does not do

`qnap-tsx70-lcd` is a small Python program that writes system information to
the 2 × 16 character display on the front of the chassis, and reacts to the two
buttons next to it.

### It does

- rotate through pages of system information;
- show per-disk SMART detail when you press the buttons;
- switch the backlight off after a period of inactivity;
- run as a systemd service and start at boot.

### It does not

- need fan control. They are separate services and the display does not depend
  on the fan one in any way.
- require a particular disk layout, filesystem or number of drives. Zero disks
  is a supported configuration.
- touch your storage. It reads SMART attributes; it never writes to a disk.
- work with QTS. This is for a machine where you have replaced QNAP's firmware
  with an ordinary Linux distribution.

### Four separate capabilities

It helps to keep these apart when something does not work, because they fail
independently:

| Capability | Needs | Fails as |
|---|---|---|
| LCD writing | serial port, correct protocol | blank or garbled display |
| Buttons | same serial port, reading direction | presses ignored |
| Sensors | `coretemp`, `acpitz`, NIC driver | `N/A` on temperature pages |
| SMART | `smartmontools`, readable disks | `N/A` on disk pages |

Only the first two are strictly the LCD. The last two degrade to `N/A` and
never stop the service.

**Tested on the TS-470 Pro only.** See
[COMPATIBILITY.md](COMPATIBILITY.md) before assuming your model works.

---

## 2. Safety

Read this section even if you skip the rest.

**You will be writing to a serial port as root.** If you name the wrong device,
you write bytes into whatever is on the other end. That could be a console, a
UPS, a modem, or a serial-attached device that does something when it receives
unexpected input. Section [4](#4-identifying-your-hardware) is about making
sure you have the right port, and section
[5](#5-testing-the-panel-before-installing-anything) tests it reversibly before
anything is installed.

**Do not run this alongside QTS.** If QNAP's firmware is still running, it owns
the panel. Two writers on one serial port produce corruption at best.

**Check nothing else is using the port.** A `getty`, a UPS daemon such as
`nut` or `apcupsd`, or another LCD project will conflict.

```bash
sudo fuser -v /dev/ttyS1          # who has it open?
systemctl list-units 'serial-getty*'
```

**Fan control is a separate decision.** Nothing in this guide touches your
fans. The default installation does not install fan control, and this guide
never asks you to. If you want it later, read
[FAN_CONTROL.md](FAN_CONTROL.md) — its calibration deliberately stops the fan.

**What gets modified.** An LCD-only install touches these places and no
others:

| Path | Change |
|---|---|
| `/usr/local/bin/qnap-tsx70-lcd` | new file |
| `/etc/qnap-tsx70-lcd.conf` | created only if absent; an existing file is never overwritten |
| `/etc/systemd/system/qnap-tsx70-lcd.service` | new file |
| `/etc/systemd/system/multi-user.target.wants/` | symlink created by `systemctl enable` |
| `/var/lib/qnap-tsx70/` | created, empty for an LCD-only install |
| your package manager | `apt-get install smartmontools util-linux` if either is missing — skip with `--skip-deps` |

If an installation from before version 2.0.0 is found, it is backed up to
`/var/backups/qnap-tsx70/<timestamp>/` before anything changes; see
[MIGRATION_FROM_SATURN.md](MIGRATION_FROM_SATURN.md).

`--with-fan-control` additionally writes `/usr/local/bin/qnap-tsx70-fancontrol`,
`/etc/systemd/system/qnap-tsx70-fancontrol.service` and
`/etc/modules-load.d/qnap-tsx70.conf`, and loads the `f71882fg` and `coretemp`
modules.

Preview the whole thing without changing anything:

```bash
sudo ./scripts/install.sh --dry-run
```

---

## 3. Prerequisites

| Requirement | Notes |
|---|---|
| A QNAP TS-x70 with the front LCD | Tested: TS-470 Pro. Others unverified. |
| Linux with systemd | Tested: Ubuntu 24.04 LTS |
| Python 3.9 or newer | Standard library only, no pip packages |
| `root` / `sudo` | Needed for the serial port and for SMART |
| `smartmontools` | Optional — disk pages show `N/A` without it |
| `util-linux` (`lsblk`) | Optional — no disks are listed without it |

```bash
sudo apt install -y python3 smartmontools util-linux
python3 --version
```

Get the repository:

```bash
git clone https://github.com/marzliak/qnap-ts470-linux.git
cd qnap-ts470-linux
```

---

## 4. Identifying your hardware

### Confirm the model

```bash
cat /sys/class/dmi/id/product_name
cat /sys/class/dmi/id/board_name
```

Some QNAP boards report an unhelpful or empty product name. If the chassis
badge says TS-x70 and there is a two-line display on the front, keep going.

### Find the serial port

The panel is on a real UART, not a USB adapter. List them:

```bash
ls -l /dev/ttyS*
```

On the reference TS-470 Pro the panel is on **`/dev/ttyS1`**, owned by
`root:dialout` with mode `0660`. Most machines list many `/dev/ttyS*` nodes;
the majority are not wired to anything.

**Do not assume `/dev/ttyS1`.** Enumeration depends on how many UARTs the
kernel finds, which varies by board and kernel version. Which port is the right
one is determined in section [5](#5-testing-the-panel-before-installing-anything)
by trying it.

Ports the kernel believes are real hardware:

```bash
sudo dmesg | grep -i 'ttyS'
# or
setserial -g /dev/ttyS* 2>/dev/null | grep -v 'unknown'
```

### Check permissions and conflicts

```bash
sudo fuser -v /dev/ttyS1
```

No output means nothing has it open, which is what you want. If a
`serial-getty@ttyS1` is running, disable it:

```bash
sudo systemctl disable --now serial-getty@ttyS1.service
```

### Run the built-in check

This is read-only. It writes nothing to the display and changes nothing:

```bash
sudo ./bin/qnap-tsx70-lcd --check
```

Sample output:

```text
ok   serial port /dev/ttyS1
ok   CPU temp sensor 'coretemp'
ok   board temp sensor 'acpitz'
ok   fan controller /sys/devices/platform/f71882fg.2592 (active channels: [3])
ok   smartctl available
ok   4 disk(s) detected
ok   7 page(s) enabled: host,cpu,uptime,cpuload,fan,net,temps
```

`warn` lines are informational — a missing sensor becomes `N/A` on the display
and nothing more. A `FAIL` line on the serial port is what stops an install.

---

## 5. Testing the panel before installing anything

**Do this before you install.** It is completely reversible, takes a minute,
and tells you whether the protocol works on your machine.

### With this project's binary

```bash
sudo ./bin/qnap-tsx70-lcd --test-message "qnap-tsx70|hello"
```

The panel should light up and show `qnap-tsx70` on the top row and `hello` on
the bottom. Text before the `|` goes on row 1, text after it on row 2.

Undo it:

```bash
sudo ./bin/qnap-tsx70-lcd --clear
```

Try a different port without editing any file:

```bash
sudo ./bin/qnap-tsx70-lcd --port /dev/ttyS2 --test-message
```

### With nothing but shell built-ins

Useful if you want to see exactly what is being sent, or to test before
trusting the program at all:

```bash
# 1200 baud, 8 data bits, no parity, 1 stop bit, raw
stty -F /dev/ttyS1 1200 cs8 -cstopb -parenb -echo raw

# backlight on
printf '\x4d\x5e\x01' > /dev/ttyS1

# row 1 and row 2, padded to exactly 16 characters each
printf '\x4d\x0c\x00\x10Hello World     ' > /dev/ttyS1
printf '\x4d\x0c\x01\x10Line 2 here     ' > /dev/ttyS1

# clear and backlight off
printf '\x4d\x28' > /dev/ttyS1
printf '\x4d\x5e\x00' > /dev/ttyS1
```

The padding is not optional. Each row write announces 16 bytes and the panel
waits for exactly that many.

### Reading what the buttons send

```bash
sudo cat /dev/ttyS1 | xxd
```

Press ENTER and SELECT. You should see 4-byte groups beginning `53 05 00`,
with the last byte `01` for ENTER, `02` for SELECT and `00` on release. Press
Ctrl-C to stop.

### What the failure modes look like

| Symptom | Most likely cause |
|---|---|
| Nothing at all, no backlight | Wrong port, or the panel is not on a serial port on your model |
| Backlight comes on, no text | Right port, right backlight command, wrong text framing — check the padding |
| Random or fragmentary characters | Wrong baud rate, or writes sent faster than 1200 baud can carry |
| Text appears then garbles | Something else is writing to the same port |
| `Permission denied` | Not running as root |
| `Device or resource busy` | Another process holds the port — `sudo fuser -v /dev/ttyS1` |

If the shell test works but this project does not, that is a bug worth
reporting. If neither works, your model probably uses a different panel; see
[COMPATIBILITY.md](COMPATIBILITY.md).

---

## 6. Installing

Once the test above works:

```bash
sudo ./scripts/install.sh
```

This is the LCD-only installation. It does not install, enable or start fan
control.

### What it does, in order

1. **Preflight.** Checks the repository files, `python3`, systemd and the
   serial port, and runs `--check`. On failure it aborts having changed
   nothing.
2. **Dependencies.** Installs `smartmontools` if missing and `apt-get` is
   available. Skip with `--skip-deps`.
3. **Migration.** An installation from before version 2.0.0 is backed up and
   migrated. See [MIGRATION_FROM_SATURN.md](MIGRATION_FROM_SATURN.md).
4. **Install.** Copies the binary, the config and the unit file.
5. **Enable and start.** Enables the service at boot and starts it.
6. **Validate.** Confirms the service is active and runs `--check` again.

If step 4, 5 or 6 fails during a migration, the installer restores the previous
installation automatically.

### Options

| Option | Effect |
|---|---|
| `--lcd` | LCD only. The default; the flag is there for clarity in scripts. |
| `--with-fan-control` | Also install the experimental fan service. Read [FAN_CONTROL.md](FAN_CONTROL.md) first. |
| `--dry-run` | Print every action, change nothing. |
| `--no-migrate` | Leave any pre-2.0.0 installation alone. |
| `--skip-deps` | Do not install distribution packages. |
| `--force` | Continue even if the serial preflight fails. |

Running the installer again is safe. It overwrites the binary and unit, keeps
your config, and restarts the service.

---

## 7. Verifying

```bash
systemctl is-active qnap-tsx70-lcd          # expect: active
systemctl is-enabled qnap-tsx70-lcd         # expect: enabled
journalctl -u qnap-tsx70-lcd -n 30
sudo qnap-tsx70-lcd --check
```

Watch the panel for about 40 seconds. With the default settings it should cycle
through seven pages at five seconds each and come back to the first.

Render a single page to standard output and to the display, then exit:

```bash
sudo qnap-tsx70-lcd --once
```

Confirm it survives a reboot:

```bash
sudo reboot
# after it comes back:
systemctl is-active qnap-tsx70-lcd
```

---

## 8. Configuration

The file is `/etc/qnap-tsx70-lcd.conf`. The shipped example
`config/qnap-tsx70-lcd.conf.example` lists every key with its default and an
explanation.

Format is `key = value`, one per line, `#` starts a comment. Unknown keys are
ignored, and a value that cannot be parsed falls back to its default rather
than stopping the service — a typo will not take your display down, but it will
also not do what you meant, so check with `--check` after editing.

**Restart after any change:**

```bash
sudo systemctl restart qnap-tsx70-lcd
```

### The keys you are most likely to change

| Key | Default | What it does |
|---|---|---|
| `serial_port` | `/dev/ttyS1` | Which device the panel is on |
| `page_interval` | `5.0` | Seconds each page stays up |
| `screen_timeout` | `600.0` | Seconds of inactivity before the backlight goes off |
| `detail_timeout` | `15.0` | Seconds before disk detail returns to rotation |
| `pages` | all seven | Which pages, in which order |
| `net_interface` | *(empty)* | Interface for the throughput page; empty follows the default route |
| `smart_enabled` | `true` | Query SMART at all |

Values may be quoted and may carry an inline comment; the last assignment of a
repeated key wins. To see exactly what the service will use — which is what
`install.sh` and `diagnose.sh` ask for, so that nothing re-implements this
grammar — run:

```bash
qnap-tsx70-lcd --print-config serial_port
```

It prints one effective value on stdout and exits nonzero, with a message on
stderr, if that key is malformed.

### Timing keys

| Key | Default | Notes |
|---|---|---|
| `write_delay` | `0.18` | Seconds after each row write. **Clamped to a minimum of 0.18** — see [section 12](#12-protocol-limits-worth-knowing) |
| `refresh_interval` | `10.0` | Seconds between metric refreshes |
| `disk_refresh_interval` | `900.0` | Seconds between disk rescans; deliberately slow because SMART queries can wake idle drives |
| `multi_press_window` | `1.5` | Window in which repeated SELECT presses count as one gesture |

### Sensor keys

Sensors are found by **driver name**, never by `hwmonN` number, because the
numbering changes between boots.

| Key | Default | Notes |
|---|---|---|
| `cpu_temp_sensor` | `coretemp` | |
| `board_temp_sensor` | `acpitz` | |
| `nic_temp_sensor` | *(empty)* | Empty auto-detects `r8169`, then `e1000` |
| `fan_device_glob` | `/sys/devices/platform/f71882fg.*` | A pattern matching more than one device is treated as no match, and the fan page shows `N/A` |

See what your machine actually exposes:

```bash
for h in /sys/class/hwmon/hwmon*; do printf '%s\t%s\n' "$h" "$(cat "$h/name")"; done
```

### Keeping the backlight on permanently

```ini
screen_timeout = 999999999
```

### Example: a quiet, minimal display

```ini
serial_port = /dev/ttyS1
pages = cpu,temps
page_interval = 10.0
screen_timeout = 120.0
smart_enabled = false
```

---

## 9. Daily use: pages and buttons

### The automatic pages

| Page | Row 1 | Row 2 |
|---|---|---|
| `host` | Hostname | IP address of the default route |
| `cpu` | CPU temperature and load average | RAM used / total, in GiB |
| `uptime` | Uptime | Number of disks online |
| `cpuload` | CPU utilisation percentage | Date and time |
| `fan` | RPM of the first turning fan channel | Second channel, or duty percentage |
| `net` | Receive throughput | Transmit throughput |
| `temps` | CPU and board temperature | NIC temperature |

CPU utilisation is the change between two `/proc/stat` samples, so it reflects
the last few seconds rather than an average since boot.

The fan page reports **the channels that are actually turning**. On the
reference machine that is channel 3. Yours may differ, and it is discovered
rather than assumed.

### Reading `N/A`

`N/A` means "this machine did not report that value", not "error". Common and
harmless causes: no NIC temperature sensor, no `smartctl`, a disk that does not
report a temperature, no fan controller found. The service keeps running.

### The buttons

| Action | Effect |
|---|---|
| **ENTER** | Enter disk detail mode; press again to cycle the pages for the current disk |
| **SELECT** | Next disk; after the last one, return to automatic rotation |
| **SELECT three times within 1.5 s** | Switch the backlight off |
| **Any button while the backlight is off** | Wake it, without performing the action |

A single SELECT is acted on when the 1.5 second window closes, because the
program cannot know whether a second press is coming.

With **no disks attached**, ENTER and SELECT do nothing and rotation continues.
This is deliberate: entering a detail mode with nothing to detail used to crash
the daemon.

### Disk detail pages

| Page | Content |
|---|---|
| 1 | Size, temperature and SMART health, plus `!` if any sectors have been reallocated |
| 2 | Device model |
| 3 | Reallocated sector count |
| 4 | Pending sector count |
| 5 | Power-on hours and power cycle count |

The device name is on the header row rather than repeated on each page. Each
row is 16 columns, so counts get a row of their own instead of being packed
together where a large raw SMART value would truncate the line.

Detail mode returns to automatic rotation after `detail_timeout` seconds
without a press.

### Sleep and wake

The backlight switches off after `screen_timeout` seconds without a button
press, or immediately on a triple SELECT. Any press wakes it. The first press
after waking only wakes — it does not also open detail mode — so you cannot
change the display by accident when you just wanted to look at it.

The panel keeps whatever was last written to it even with the backlight off.
Waking shows the current page at the next refresh.

---

## 10. Running it under systemd

```bash
systemctl status qnap-tsx70-lcd
sudo systemctl start qnap-tsx70-lcd
sudo systemctl stop qnap-tsx70-lcd
sudo systemctl restart qnap-tsx70-lcd
sudo systemctl enable qnap-tsx70-lcd     # start at boot
sudo systemctl disable qnap-tsx70-lcd    # do not start at boot
```

### Logs

```bash
journalctl -u qnap-tsx70-lcd -f            # follow
journalctl -u qnap-tsx70-lcd -n 100        # last 100 lines
journalctl -u qnap-tsx70-lcd -b -1         # previous boot
journalctl -u qnap-tsx70-lcd --since '1 hour ago'
```

A healthy service is quiet. It logs at startup and on errors, not per page.

### Confirming it holds the port

```bash
sudo fuser -v /dev/ttyS1
sudo ls -l /proc/$(systemctl show -p MainPID --value qnap-tsx70-lcd)/fd | grep ttyS
```

### Stopping cleanly

The unit has `ExecStopPost` that clears the display and switches the backlight
off, so stopping the service does not leave stale text on the panel.

### About the unit's hardening

The unit sets `ProtectSystem=strict`, `ProtectKernelTunables`,
`NoNewPrivileges`, a system-call filter and related options. These reduce the
filesystem and kernel surface the service can reach.

To be clear about what that is **not**: the service still runs as **root**,
because `smartctl` needs raw access to the block devices it queries. The
hardening does not reduce its privileges over your storage. If you would rather
it did not have that access, set `smart_enabled = false` in the config — the
disk pages then show `N/A`.

---

## 11. Customising the pages

### Reordering and removing

```ini
pages = cpu,temps,net
```

List the available names:

```bash
qnap-tsx70-lcd --list-pages
```

Unknown names are ignored. Listing a page twice shows it twice per cycle, which
is a legitimate way to give one page more screen time.

### Writing a new page

Pages are small functions in `bin/qnap-tsx70-lcd` that take a snapshot
dictionary and return two strings:

```python
def page_swap(s):
    return ("Swap", "%s MiB" % fmt_value(s.get("swap_used")))
```

Register it in the `PAGES` dictionary:

```python
PAGES = {
    ...
    "swap": page_swap,
}
```

Then add `swap` to `pages` in your config. Two rules:

- **16 characters per row.** Anything longer is truncated, not wrapped.
- **Never raise.** Use `s.get(...)` and the `fmt_*` helpers, which already
  return `N/A` for missing values. An exception in a page function stops the
  service.

The test suite renders every registered page against a completely empty
snapshot, so a new page is checked automatically for the missing-data case:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

### Characters

7-bit ASCII only. Anything else is replaced with `?` before transmission, so a
source character always costs exactly one column and never desynchronises the
frame. Accented characters and box-drawing will not render.

---

## 12. Protocol limits worth knowing

Full detail in [LCD_PROTOCOL.md](LCD_PROTOCOL.md). The parts that affect daily
use:

- **2 rows of 16 characters.** No scrolling, no cursor addressing, no partial
  updates — a write replaces a whole row.
- **1200 baud.** One row write takes about 167 ms on the wire. This is why
  `write_delay` is clamped to a minimum of 0.18 s: writing faster than the line
  can carry is what produces garbled text.
- **Both rows cost about a third of a second.** A page interval below roughly
  0.5 s is not achievable, and 3–5 s is what actually reads well.
- **No scrolling marquee.** A 16-column scroll would need one 167 ms write per
  frame. Page rotation is the practical option.
- **Backlight is on or off.** There is no brightness or contrast control on the
  A125. A dim panel is an aged backlight, not a setting.
- **The panel cannot be queried.** It reports neither its contents nor its
  model, which is why section [5](#5-testing-the-panel-before-installing-anything)
  is a manual test rather than automatic detection.

---

## 13. Troubleshooting by symptom

### The display is completely blank, no backlight

```bash
systemctl is-active qnap-tsx70-lcd
journalctl -u qnap-tsx70-lcd -n 50
sudo qnap-tsx70-lcd --check
```

If the service is running and the check passes, test the panel directly with
`--test-message` ([section 5](#5-testing-the-panel-before-installing-anything)).
If that does nothing either, you likely have the wrong port. If the backlight
simply timed out, press a button.

### Backlight is on but there is no text

The backlight command works and the text framing does not. Usually a wrong
`write_delay` or another process writing to the same port.

```bash
sudo fuser -v /dev/ttyS1
```

Test the raw sequence from [section 5](#5-testing-the-panel-before-installing-anything).
If the raw write works but the service does not, file an issue with a
diagnostic bundle.

### Text is garbled or fragmentary

Almost always timing or contention.

1. Confirm `write_delay` is at least `0.18`.
2. Confirm the port is at 1200 baud: `stty -F /dev/ttyS1 -a | head -1`.
3. Check for a second writer: `sudo fuser -v /dev/ttyS1`.
4. Make sure no service from a pre-2.0.0 installation survived; the check is
   in [MIGRATION_FROM_SATURN.md](MIGRATION_FROM_SATURN.md#5-verifying-the-migration).

### Buttons do nothing

```bash
sudo systemctl stop qnap-tsx70-lcd
sudo cat /dev/ttyS1 | xxd     # press the buttons; expect 53 05 00 xx
sudo systemctl start qnap-tsx70-lcd
```

If no bytes arrive, the panel is not sending them and this is a hardware or
model difference. If bytes arrive but nothing happens, check that you have
disks — with none attached the buttons are intentionally inert.

Remember a single SELECT is acted on only after the 1.5 s multi-press window
closes.

### The service restarts over and over

```bash
journalctl -u qnap-tsx70-lcd -n 100
```

The usual causes are a serial port that disappeared, a port held by another
process, or a permissions change. The log says which.

`Restart=always` with `RestartSec=5` retries every five seconds, but the unit
also sets `StartLimitBurst=10` over `StartLimitIntervalSec=300`: after ten
starts in five minutes systemd gives up and leaves the unit `failed`, rather
than retrying a permanently broken configuration forever. Once you have fixed
the cause, clear that state before starting it again:

```bash
sudo systemctl reset-failed qnap-tsx70-lcd
sudo systemctl start qnap-tsx70-lcd
```

### Temperatures show N/A

```bash
for h in /sys/class/hwmon/hwmon*; do printf '%s\t%s\n' "$h" "$(cat "$h/name")"; done
sudo modprobe coretemp
```

If your machine names its sensors differently, set `cpu_temp_sensor`,
`board_temp_sensor` or `nic_temp_sensor` to the names shown.

### The fan page shows N/A

Either the `f71882fg` module is not loaded, or more than one matching device
exists and the program refuses to guess between them.

```bash
lsmod | grep f71882fg
sudo modprobe f71882fg
ls -d /sys/devices/platform/f71882fg.*
```

If several appear, set `fan_device_glob` to a pattern matching exactly one.
Note that the fan **page** only reads the controller; it does not need the fan
control service.

### No disks are listed

```bash
which lsblk smartctl
lsblk -d -o NAME,SIZE,TYPE
```

`lsblk` missing means no disks are listed at all. `smartctl` missing means
disks are listed without SMART detail. USB-attached disks often report no SMART
data through their bridge, which is normal.

### `Device or resource busy`

```bash
sudo fuser -v /dev/ttyS1
systemctl list-units 'serial-getty*'
sudo systemctl disable --now serial-getty@ttyS1.service
```

### Everything worked until a kernel upgrade

Sensor paths are resolved by driver name, so a renumbered `hwmonN` is handled.
A changed `/dev/ttyS*` enumeration is not — recheck with `--check` and update
`serial_port`.

---

## 14. Upgrading

```bash
cd qnap-ts470-linux
git pull
sudo ./scripts/install.sh
```

The installer is idempotent: it replaces the binary and the unit, **keeps your
config**, and restarts the service. New configuration keys appear in
`config/qnap-tsx70-lcd.conf.example`; compare it with your file after
upgrading:

```bash
diff <(grep -vE '^\s*(#|$)' config/qnap-tsx70-lcd.conf.example) \
     <(grep -vE '^\s*(#|$)' /etc/qnap-tsx70-lcd.conf)
```

Coming from a pre-2.0.0 layout is a migration rather than an upgrade; see
[MIGRATION_FROM_SATURN.md](MIGRATION_FROM_SATURN.md).

---

## 15. Rolling back

### To a previous commit

```bash
cd qnap-ts470-linux
git log --oneline
git checkout <commit>
sudo ./scripts/install.sh
```

### After a failed migration

The installer rolls back automatically. To do it by hand, the backup directory
it reported contains the previous units, binaries and cache:

```bash
ls /var/backups/qnap-tsx70/
```

The procedure is in
[MIGRATION_FROM_SATURN.md](MIGRATION_FROM_SATURN.md#4-rolling-back).

### Just stop it

```bash
sudo systemctl disable --now qnap-tsx70-lcd
sudo qnap-tsx70-lcd --clear
```

Nothing is removed and re-enabling restores it.

---

## 16. Uninstalling

```bash
sudo ./scripts/uninstall.sh
```

Stops and disables the services, clears the display, and removes the binaries
and unit files. **Kept by default:** `/etc/qnap-tsx70-lcd.conf`,
`/var/lib/qnap-tsx70/` and `/var/backups/qnap-tsx70/`.

Remove those too:

```bash
sudo ./scripts/uninstall.sh --purge
```

Preview first:

```bash
sudo ./scripts/uninstall.sh --dry-run
```

Kernel modules are left loaded. Remove them manually if you want:

```bash
sudo modprobe -r f71882fg
```

---

## 17. Collecting a diagnostic bundle

```bash
sudo ./scripts/diagnose.sh -o bundle.txt
```

Include SMART summaries as well:

```bash
sudo ./scripts/diagnose.sh --with-smart -o bundle.txt
```

Writing to a file is all-or-nothing: the bundle is collected and redacted into
a temporary file next to the target, set to mode 0600 and only then renamed
into place. If anything fails — a directory that does not exist, a path that
is a directory, no permission, a redactor that cannot run — the command exits
nonzero, says why, leaves no partial file, and does **not** report success.

The bundle contains the distribution and kernel, chassis model, CPU and memory
totals, installed versions, loaded modules, serial ports and permissions, hwmon
sensor names, fan controller state, your configuration, the disk list, service
status and recent logs.

### What is redacted

Hostnames, IPv4 and IPv6 addresses, MAC addresses, UUIDs and anything labelled
as a serial number are replaced with placeholders. IPv6 covers the compressed
forms as well — `::1`, `fe80::1%eth0`, `2001:db8::/32` and IPv4-mapped
addresses — because a filter that only recognised the expanded form would let
every address a real machine actually logs straight through. IP and MAC addresses are
never collected in the first place, and the DMI fields read are limited to
vendor and model — `product_uuid` and `board_serial` are never touched.

**Read the file before you post it.** Redaction is a safety net, not a
guarantee, and only you know what is sensitive on your machine.

---

## 18. Contributing a compatibility report

Reports from models other than the TS-470 Pro are the most useful thing you can
contribute, and a negative result counts.

Checklist:

- [ ] Exact model, including whether it is a `Pro`
- [ ] Distribution and `uname -r`
- [ ] Output of `sudo qnap-tsx70-lcd --check`
- [ ] Did text appear on the display?
- [ ] Did ENTER and SELECT work?
- [ ] Which sensors resolved, and which showed `N/A`
- [ ] A diagnostic bundle from `scripts/diagnose.sh`
- [ ] **Reviewed the bundle** for anything you would rather not publish

Do not include serial numbers, MAC addresses, IP addresses, hostnames, or raw
SMART output containing device identifiers.

If you are sending a pull request, run the test suite first — it needs no
hardware:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
bash -n scripts/*.sh
```

See [COMPATIBILITY.md](COMPATIBILITY.md) for the matrix your report updates.
