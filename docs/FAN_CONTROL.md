# Fan control (experimental, opt-in)

> **Experimental, and not yet run on hardware.** The register behaviour this
> depends on was observed on one machine, but **this implementation has never
> been executed on a real TS-x70**. It writes directly to your chassis fan
> controller and its calibration step **deliberately stops the fan**.
>
> You do not need it to use the LCD. If you only want the display, follow
> [LCD_GUIDE.md](LCD_GUIDE.md) and stop reading here. The default installation
> does not include fan control.

---

## 1. What this is

`qnap-tsx70-fancontrol` drives the chassis fan from CPU temperature, using the
Super-I/O monitoring chip that the mainline `f71882fg` driver exposes on
MAHOBAY-based QNAP boards.

It exists because the standard `fancontrol` package does not work on this
hardware: `f71882fg` publishes its `pwm*` attributes under the platform device
(`/sys/devices/platform/f71882fg.<addr>/`) rather than under
`/sys/class/hwmon/hwmonN/`, which is where `fancontrol` looks.

### Status by claim

Two different things get confused here, so they are kept apart. What the
*hardware* does was observed on a real machine. What *this program* does has
only been exercised offline.

| Claim | Status |
|---|---|
| `fanN_input`, `pwmN` and `pwmN_enable` semantics, and the 1 = manual / 2 = automatic mode numbering | Observed on the reference TS-470 Pro, under the previous implementation |
| That the reference unit turns channel 3 and exposes three channels | Observed on the reference TS-470 Pro |
| **This 2.0.0 implementation, on real TS-x70 hardware** | **Not yet exercised.** Validated offline only: unit tests plus simulated-controller runs of the safe-state, temperature-loss, dead-tachometer, stall-cap and signal paths |
| Calibration measures usable stall and restart duties on a real fan | **Unverified for this implementation.** The v1 code that this replaces did produce usable values on one fan |
| Works on any other TS-x70 model | **Unverified** |
| Safe for unattended use | **Not claimed** |

The offline validation is real and reproducible — interrupting a simulated
calibration with `SIGTERM`, `SIGINT`, `SIGHUP` or `SIGQUIT` leaves every
channel at full duty in automatic mode, and a dead tachometer makes the loop
escalate, give up and exit nonzero. What it cannot tell you is how *your* fan
behaves when the duty actually drops.

---

## 2. Known limitations

Read these before deciding to use it.

1. **It controls from CPU temperature only.** On a NAS the chassis fan mostly
   cools the *disks*. Four warm drives under an idle CPU will currently produce
   a low fan speed. If your workload is disk-heavy and CPU-light, this
   controller is not a good fit as-is.
2. **Single-machine calibration.** The stall and restart duties are measured on
   your fan, which is good, but the measurement procedure itself has only been
   exercised on one machine and one fan.
3. **The duty floor is a safety bias, not a measurement.** During normal
   operation the program never commands a duty below `PWM_FLOOR` (64), even if
   calibration measured a lower stall point: a fan parked at its own stall
   threshold has no margin for dust, wear or a warm day. Calibration itself is
   the exception — it has to go below the floor and to zero to find where the
   fan stops, which is the whole hazard described in [§6](#6-calibration-hazard).
4. **The chip's own automatic mode is the fallback, not a guarantee.** Every
   failure path hands the fan back to `pwm*_enable = 2`. What that mode does is
   a property of your board's firmware, not of this program.
5. **No disk temperature input, no hysteresis, no PID.** The curve is a
   straight line between two temperatures.

---

## 3. How the temperature and duty registers work

The `f71882fg` driver exposes, per channel `N`:

| Attribute | Meaning |
|---|---|
| `fanN_input` | measured speed in RPM |
| `pwmN` | commanded duty, 0–255 |
| `pwmN_enable` | control mode |

`pwmN_enable` values used here:

| Value | Meaning | Status |
|---|---|---|
| `1` | manual — `pwmN` is obeyed directly | Verified on the reference unit |
| `2` | automatic — the chip controls the fan from its own temperature input | Verified on the reference unit |

These match the mode numbering documented for the driver in the kernel tree
([`Documentation/hwmon/f71882fg.rst`](https://www.kernel.org/doc/html/latest/hwmon/f71882fg.html)).
Mode numbering above `2` is **not** used by this project.

### Channel numbering is not portable

The reference TS-470 Pro exposes three tachometers and **only channel 3 is
turning**; channels 1 and 2 read 0 RPM and sit in automatic mode. Earlier
revisions of this repository described channel 2 as the chassis fan. That was
true of one machine at one point in time and is not a general fact.

Nothing in this project hard-codes a channel. Channels are discovered by
reading the tachometers, and `status` shows you what your machine reports.

---

## 4. Installing

Fan control is never installed by default:

```bash
sudo ./scripts/install.sh --with-fan-control
```

This installs the binary and the unit, enables the unit, and **does not start
it** — because `run` requires a calibration that only you can authorise.

The unit file is `systemd/qnap-tsx70-fancontrol.service`.

---

## 5. Commands

Every mode is an explicit subcommand. There are no flags that silently change
what the program does.

### `status` — read-only

```bash
qnap-tsx70-fancontrol status
qnap-tsx70-fancontrol status --json
```

Reports the discovered controller, the temperature sensor, every channel with
its RPM, duty and mode, and the stored calibration. It writes nothing and does
not need root. Run this first, always.

### `calibrate` — hazardous, interactive

```bash
qnap-tsx70-fancontrol calibrate --yes
```

See [§6](#6-calibration-hazard) before running it. Run it as root: without
`--yes` it prints the hazard notice and exits with status 2 without touching
anything, and without root it refuses before reaching that point.

| Flag | Effect |
|---|---|
| `--yes` | required; confirms you accept the reduced-airflow risk |
| `--force` | recalibrate even when the stored calibration already matches |
| `--probe` | also spin up channels that currently read 0 RPM |
| `--channels N [N...]` | calibrate these channels instead of detecting |
| `--dry-run` | go through the motions without writing any register |

### `run` — the control loop

```bash
sudo systemctl start qnap-tsx70-fancontrol
```

This is what the service runs. It requires a stored calibration and exits
nonzero if there is none. Useful flags: `--interval`, `--temp-min`,
`--temp-max`, `--dry-run`.

### `safe-state` — recovery

```bash
sudo qnap-tsx70-fancontrol safe-state
```

Forces every controllable channel to full duty and then back to the chip's
automatic mode. Run this if anything ever leaves your fan in a state you do not
like. It is also wired as `ExecStopPost=` on the service, so a `SIGKILL` or an
OOM kill still ends with the chip in charge.

Unlike `run` and `calibrate`, this command deliberately does **not** take the
lock, so it always works as an emergency recovery. That means it can fight a
running control loop over the same registers — stop the service first unless
this genuinely is a recovery:

```bash
sudo systemctl stop qnap-tsx70-fancontrol
```

---

## 6. Calibration hazard

**Calibration deliberately stops the chassis fan.**

To find the duty at which the fan stalls, and the duty needed to restart it
from standstill, the procedure lowers the duty step by step until the fan
stops, then raises it again until it starts. During that window the machine has
**reduced or no airflow**, for up to 45 seconds at a stretch.

### Before you run it

- Let the machine idle and cool down first. Calibration refuses to start above
  **60 °C**.
- Do not run it under sustained CPU or disk load.
- Stay at the console. Do not walk away.
- Know how to power the machine off.

### Guards that are implemented

| Guard | Behaviour |
|---|---|
| Hot start | Refuses to begin above 60 °C |
| Thermal abort | Aborts and fails safe at 70 °C during the sweep |
| Stall cap | Aborts if a fan sits at 0 RPM longer than 45 s |
| Signals | `SIGTERM`, `SIGINT` (Ctrl-C), `SIGHUP` (SSH disconnect) and `SIGQUIT` all fail safe |
| Lock | An interactive calibration and the service cannot run at once |
| Degenerate results | A sweep on a fan that never turned is discarded, not saved |
| Exit path | Success, failure, exception and signal all force full duty plus automatic mode, then read the register back to confirm |

`SIGHUP` matters more than it sounds: calibrating over SSH and losing the
connection is the most likely real-world interruption.

### What is not guarded

- A fan that is already failing may not restart. If it does not restart even at
  full duty, calibration aborts with an error — but the fan is still stopped
  and that is a hardware problem, not something software can fix.
- Ambient temperature is not measured. The 60 °C and 70 °C limits are CPU
  package temperatures.

---

## 7. Fail-safe behaviour

| Condition | Response |
|---|---|
| Temperature unreadable | Full duty immediately; after 3 consecutive cycles, exit nonzero |
| 3 consecutive register write failures | Safe state, exit nonzero |
| Fan not turning | Kickstart at the calibrated restart duty; escalate to full duty after 2 attempts; after 6, exit nonzero |
| Controller ambiguous or missing | Refuse to start; no register is written |
| No calibration stored | Refuse to start |
| Corrupt or hand-edited calibration | Values re-clamped to the duty floor on load |
| `SIGTERM` from systemd | Safe state, exit 0 |
| Unhandled exception | Safe state, exit nonzero |
| `SIGKILL` / OOM kill | `ExecStopPost=` runs `safe-state` |

"Exit nonzero" is deliberate: the service is configured with `Restart=always`
and a start limit of 5 attempts per 5 minutes. A machine that genuinely cannot
control its fans stops retrying and stays visible as a failed unit, with the
chip's automatic mode in charge — rather than looping silently forever.

---

## 8. The curve

```
duty
 255 |                                   ,--------
     |                              ,---'
     |                         ,---'
 min |------------------------'
     +----+--------------------+----------+------- temp
         30 °C                70 °C
```

Below `--temp-min` (default 30 °C) the fan runs at its calibrated minimum, at
or above `--temp-max` (default 70 °C) at full duty, and in between it
interpolates linearly.

---

## 9. Troubleshooting

**`no fan controller matched /sys/devices/platform/f71882fg.*`**
The driver is not loaded. `sudo modprobe f71882fg`. If that fails, your board
may use a different Super-I/O chip; this project does not support it.

**`ambiguous fan controller: N devices match`**
More than one instance is present. The program refuses to guess. Identify the
right one and pass `--device-glob` with a pattern that matches exactly one.

**`no calibration at /var/lib/qnap-tsx70/fan-calibration.json`**
Expected before the first calibration. See [§6](#6-calibration-hazard).

**`no controllable fan channel is spinning`**
Passive detection found nothing turning. Either the fan is stopped or its
tachometer is not reporting. Check `status`, then consider `--probe` or
`--channels`.

**`another qnap-tsx70-fancontrol instance holds ...`**
The service is running. `sudo systemctl stop qnap-tsx70-fancontrol` first.

**The service keeps restarting then gives up**
Read the reason: `journalctl -u qnap-tsx70-fancontrol -n 50`. The give-up is
intentional — see [§7](#7-fail-safe-behaviour).

**The fan is loud after an error**
That is the safe state working as designed. Investigate the logged reason, then
`sudo qnap-tsx70-fancontrol safe-state` hands control back to the chip.

---

## 10. Uninstalling just fan control

```bash
sudo systemctl disable --now qnap-tsx70-fancontrol
sudo rm /etc/systemd/system/qnap-tsx70-fancontrol.service
sudo rm /usr/local/bin/qnap-tsx70-fancontrol
sudo systemctl daemon-reload
```

Stopping the service restores the chip's automatic mode. The LCD service is
independent and is unaffected.

---

## 11. Contributing evidence

Fan control needs validation on more than one machine. If you run it, the most
useful things to report are your model, the output of
`qnap-tsx70-fancontrol status --json`, your stored calibration, and whether the
fan restarted reliably. Use `scripts/diagnose.sh` — it redacts identifying
values. See [COMPATIBILITY.md](COMPATIBILITY.md).
