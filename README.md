# QNAP TS-470 Pro — Running Linux in 2026

How to breathe new life into your old QNAP TS-470 in 2026. Install Linux alongside or instead of QTS, bring the front LCD back to life, and unlock full hardware monitoring. The hardware is still great — this guide makes sure you can keep using it.

Tested on TS-470 Pro. Likely compatible with: TS-470, TS-670, TS-670 Pro, TS-870, TS-870 Pro, TS-470U-RP. This repo documents the LCD serial protocol, the saturn-lcd monitoring daemon, and hardware sensors.

---

## Hardware

| Component | Detail |
|-----------|--------|
| Chassis | QNAP TS-470 Pro |
| Motherboard | Intel MAHOBAY (DQ77MK) |
| CPU | Intel Core i3/i5/i7 — 3rd Gen IvyBridge |
| NIC | Intel 82579LM Gigabit |
| OS | Ubuntu (headless) |
| Super I/O | Fintek F71869A @ ISA 0xa20 |
| LCD | ICP A125 board, 2x16 character display |
| Serial Port | `/dev/ttyS1` @ 1200 baud 8N1 |
| Sensors | coretemp (CPU), acpitz (board), r8169 (NIC), Fintek (voltages/thermistors) |
| Fan | Rear chassis fan on Fintek channel 2 (fan2/pwm2) |

---

## Boot Sequence

Everything is automatic. Zero user interaction required.

```
1. Kernel loads f71882fg + coretemp modules
2. saturn-fan  →  calibrates fans (or loads cache), then runs temperature control loop
3. saturn-lcd  →  starts LCD display monitor
```

### Systemd Services

| Service | Type | Description |
|---------|------|-------------|
| `saturn-fancontrol` | daemon | Fan calibration on first run, then temperature control loop |
| `saturn-lcd` | daemon | LCD display with auto-rotate and button navigation |

Dependencies: `saturn-fancontrol` → `saturn-lcd`

```bash
# Check all services
systemctl is-active saturn-fancontrol saturn-lcd

# Manual control
systemctl stop saturn-lcd
systemctl start saturn-lcd
systemctl restart saturn-lcd
journalctl -u saturn-lcd -f    # view logs
journalctl -u saturn-fancontrol -f    # view fan control logs
```

---

## Install

```bash
apt install -y lm-sensors smartmontools
bash install.sh
```

Installs both services, enables them at boot, and runs the first calibration automatically.

---

## LCD Protocol (ICP A125)

### Serial Configuration

```
Port: /dev/ttyS1
Baud: 1200
Data: 8 bits, no parity, 1 stop bit (8N1)
```

### Commands

All commands start with `0x4D` prefix.

| Command | Bytes | Description |
|---------|-------|-------------|
| Backlight ON | `4D 5E 01` | Turn LCD backlight on |
| Backlight OFF | `4D 5E 00` | Turn LCD backlight off |
| Write Line 1 | `4D 0C 00 10` + 16 chars | Write to first line |
| Write Line 2 | `4D 0C 01 10` + 16 chars | Write to second line |
| Write Both | `4D 0C 00 20` + 32 chars | Write both lines (first write only) |
| Clear Display | `4D 28` | Clear screen content |
| Stop Auto-Clock | `4D 0D` | Stop built-in clock display |

**Important**: Writing both lines with `0x20` (32 chars) only works on the initial write. For subsequent updates, write each line separately with `0x10` (16 chars).

### Button Input

Buttons send 4 bytes on the same serial port:

```
0x53 0x05 0x00 {bitmask}
```

| Bitmask | Button |
|---------|--------|
| `0x01` | ENTER |
| `0x02` | SELECT |
| `0x03` | Both pressed |
| `0x00` | Both released |

**Note**: The USB Copy button on the front panel is NOT accessible via serial. It is likely connected to GPIO or the IT8528E EC.

### Quick Test

```bash
stty -F /dev/ttyS1 1200 cs8 -cstopb -parenb -echo raw
echo -ne '\x4d\x5e\x01' > /dev/ttyS1                       # backlight on
echo -ne '\x4d\x0c\x00\x10Hello World     ' > /dev/ttyS1   # line 1
echo -ne '\x4d\x0c\x01\x10Line 2 here     ' > /dev/ttyS1   # line 2
```

### Timing Constraints

At 1200 baud (8N1 = 10 bits/byte):
- 1 byte = ~8.3ms
- 1 line write (20 bytes) = ~167ms
- Both lines = ~333ms
- **Minimum delay between writes**: 180ms
- **Minimum page interval**: ~500ms (practical: 3-5 seconds)

Character-by-character scrolling is not viable at 1200 baud — use page-based rotation instead.

### Brightness

The A125 board has **no brightness/contrast control** — only backlight on/off. Tested commands `0x5F`, `0x5D`, `0x5C`, `0x5B` with various values — none affected brightness. Dim backlight is due to LED/CCFL aging.

---

## Saturn LCD Service

Script: `/usr/local/bin/saturn-lcd`

### Auto-Rotate Mode (7 screens, 5 seconds each)

| Screen | Line 1 | Line 2 |
|--------|--------|--------|
| 1 | Hostname (from OS) | IP address |
| 2 | CPU temp + load avg | RAM used/total (dot thousands) |
| 3 | Uptime (Xh Xm) | N disks online |
| 4 | CPU usage % | Date/time |
| 5 | Fan1: RPM | Fan2: RPM + duty % |
| 6 | Net Rx rate | Net Tx rate |
| 7 | CPU + Board temp | NIC temp |

Hardware info refreshes every 10 seconds. CPU usage is calculated as delta between `/proc/stat` readings (real-time, not since boot).

### Detail Mode (via buttons)

- **SELECT**: Enter detail mode, cycle through disks, return to auto after last disk
- **ENTER**: Cycle detail pages for current disk
- **Timeout**: Returns to auto mode after 15 seconds of inactivity

Detail pages per disk:

| Page | Content |
|------|---------|
| 1 | `sdX SIZE TEMPdegC HEALTH` (+ `R:N!` warning if reallocated > 0) |
| 2 | Device model |
| 3 | Power-on hours, reallocated sectors, pending sectors |
| 4 | Power cycle count |

### Screen Power Management

| Action | Behavior |
|--------|----------|
| Triple-click SELECT (3 presses in 1.5s) | Toggle LCD backlight on/off |
| 10 minutes inactivity | LCD turns off automatically |
| Any button press while off | Wakes LCD (wake only, no action executed) |

### Systemd Service

```bash
cat > /etc/systemd/system/saturn-lcd.service << 'SVC'
[Unit]
Description=Saturn LCD Monitor
After=multi-user.target saturn-fancontrol.service

[Service]
Type=simple
ExecStart=/usr/local/bin/saturn-lcd
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable saturn-lcd
systemctl start saturn-lcd
```

---

## Fan Control

### Hardware

The rear chassis fan connects to **Fintek channel 2** (`fan2_input` / `pwm2`). Channels 1 and 3 have no fans connected.

> **Note**: The `fancontrol` system package does not work with this hardware — the f71882fg driver exposes sysfs files directly under the device path, not under `/sys/class/hwmon/hwmonX/`. `saturn-fancontrol` is a self-contained Python daemon that reads the same sysfs files directly, with no system package dependencies beyond Python 3.

Calibrated values (auto-detected per fan):

| PWM Value | RPM | Note |
|-----------|-----|------|
| 255 (max) | ~1564 | Full speed |
| 160 | ~940 | Normal operation |
| 75 | ~432 | Minimum safe speed |
| 65 | ~376 | Restart threshold |
| 60 | 0 | Stall point |

**Important**: Fan stalls below PWM ~60 and requires PWM 65+ to restart from standstill.

### Manual Control

```bash
P=/sys/devices/platform/f71882fg.2592

# Read current state
cat $P/fan2_input      # RPM
cat $P/pwm2            # duty cycle (0-255)
cat $P/pwm2_enable     # 1=manual, 2=auto

# Set manual mode and speed
echo 1 > $P/pwm2_enable
echo 200 > $P/pwm2

# Restore automatic (hand back to saturn-fan)
echo 2 > $P/pwm2_enable
```

### Auto-Calibration (`saturn-fancontrol`)

Script: `/usr/local/bin/saturn-fan-calibrate`

On first boot (or when fans change), the script:

1. **Checks cache** (`/etc/saturn-fan-cache.json`) — if fan config hasn't changed, skips calibration (~5 seconds)
2. **Detects active fans** — spins up each channel to find connected fans
3. **Calibrates each fan** — finds max RPM, stall point (MINSTOP), restart threshold (MINSTART)
4. **Runs temperature control loop** — linear curve 30–70°C → MINPWM–255

Recalibration triggers automatically when:
- No cache file exists (first run)
- Fan channels changed (fan added/removed/moved)

Force recalibration:
```bash
saturn-fan-calibrate --force
```

Calibrate only (no control loop):
```bash
saturn-fan-calibrate --calibrate-only
```

### Temperature Curve

| CPU Temp | PWM | Behavior |
|----------|-----|----------|
| ≤ 30°C | MINPWM (calibrated) | Minimum speed |
| 30–70°C | Linear interpolation | Proportional to temp |
| ≥ 70°C | 255 | Full speed |

### Systemd Service

```bash
cat > /etc/systemd/system/saturn-fancontrol.service << 'SVC'
[Unit]
Description=Saturn Fan Control (calibration + temperature loop)
After=multi-user.target

[Service]
Type=simple
ExecStartPre=/sbin/modprobe f71882fg
ExecStartPre=/sbin/modprobe coretemp
ExecStart=/usr/local/bin/saturn-fan-calibrate
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable saturn-fan
systemctl start saturn-fan
```

---

## Hardware Sensors

### Kernel Modules

```bash
modprobe coretemp
modprobe f71882fg
```

Loaded automatically at boot via `/etc/modules`:
```
coretemp
f71882fg
```

### Available Sensors

| Sensor | Path | Values |
|--------|------|--------|
| CPU Package + 4 cores | `/sys/class/hwmon/hwmon*/temp1_input` (coretemp) | millidegrees C |
| Board (ACPI) temp1/2 | `/sys/class/hwmon/hwmon*/temp*_input` (acpitz) | millidegrees C |
| NIC (r8169) | `/sys/class/hwmon/hwmon*/temp1_input` (r8169) | millidegrees C |
| Fan2 RPM | `/sys/devices/platform/f71882fg.2592/fan2_input` | RPM |
| Fan2 PWM | `/sys/devices/platform/f71882fg.2592/pwm2` | 0–255 |
| Fintek voltages | via `sensors` command | 3.3V, 3VSB, Vbat, etc. |
| Fintek thermistors | via `sensors` command | 3 zones |

### Fintek F71869A

Detected at ISA 0xa20. Runtime sysfs at:

```
/sys/devices/platform/f71882fg.2592/
```

Files: `fan{1,2,3}_input`, `pwm{1,2,3}`, `pwm{1,2,3}_enable`, `in{0-8}_input`, `temp{1-3}_input`

> Note: hwmon numbering (`hwmon0`, `hwmon1`, ...) is not stable across boots and varies by kernel version. Always reference sensors by driver name, not hwmon number.

---

## Files

| Path | Description |
|------|-------------|
| `/usr/local/bin/saturn-lcd` | LCD monitor daemon (Python) |
| `/usr/local/bin/saturn-fan-calibrate` | Fan calibration + control daemon (Python) |
| `/etc/saturn-fan-cache.json` | Calibration cache |
| `/etc/systemd/system/saturn-fancontrol.service` | Fan control systemd service |
| `/etc/systemd/system/saturn-lcd.service` | LCD systemd service |
| `/etc/modules` | Kernel modules (coretemp, f71882fg) |

---

## Notes

- The original QNAP fan died and was replaced. The replacement connects to Fintek channel 2.
- Fan channels 1 and 3 have no fans connected (0 RPM).
- The `fancontrol` system package does not work on this hardware — use `saturn-fancontrol` instead.
- The USB Copy front panel button is not accessible via the A125 serial protocol or Linux input subsystem. It likely requires the IT8528E EC.
- The A125 LCD backlight has no adjustable brightness — only on/off. Dim appearance is normal for aged units.

---

## Compatible Models

The LCD protocol and sensor layout likely applies to other MAHOBAY-based QNAP models:

- TS-470, TS-470 Pro
- TS-670, TS-670 Pro
- TS-870, TS-870 Pro
- TS-470U-RP

If you have one of these and want to contribute findings, PRs are welcome.

---

## References

- [QNAP TS-453 Pro LCD/LEDs/fan/buttons (gist)](https://gist.github.com/zopieux/0b38fe1c3cd49039c98d5612ca84a045)
- [LCDProc icp_a106 driver](https://github.com/lcdproc/lcdproc/blob/master/server/drivers/icp_a106.c)
- [qnap8528 kernel module](https://github.com/0xGiddi/qnap8528)
- [QNAP-EC](https://github.com/Stonyx/QNAP-EC)
- [Unraid LCD_Manager plugin](https://forums.unraid.net/topic/136952-plugin-lcd_manager/)
- [Unraid QNAP-EC plugin](https://github.com/ich777/unraid-qnapec)
