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
| OS | Linux (tested: Ubuntu 24.04 LTS headless) |
| LCD | ICP A125 board, 2×16 character display |
| Serial Port | `/dev/ttyS1` @ 1200 baud 8N1 |
| Super I/O | Fintek F71869A @ ISA 0xa20 |

---

## LCD Protocol (ICP A125)

### Serial Configuration

```
Port:  /dev/ttyS1
Baud:  1200
Data:  8 bits, no parity, 1 stop bit (8N1)
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

> **Important**: Writing both lines with `0x20` only works on the initial write. For subsequent updates, write each line separately with `0x10`.

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

> **Note**: The USB Copy button on the front panel is NOT accessible via serial. It is likely connected to GPIO or the IT8528E EC.

### Quick Test

```bash
stty -F /dev/ttyS1 1200 cs8 -cstopb -parenb -echo raw
echo -ne '\x4d\x5e\x01' > /dev/ttyS1                       # backlight on
echo -ne '\x4d\x0c\x00\x10Hello World     ' > /dev/ttyS1   # line 1 (pad to 16 chars)
echo -ne '\x4d\x0c\x01\x10Line 2 here     ' > /dev/ttyS1   # line 2
```

### Timing Constraints

At 1200 baud (8N1 = 10 bits/byte):

| Operation | Duration |
|-----------|----------|
| 1 byte | ~8.3ms |
| 1 line write (20 bytes) | ~167ms |
| Both lines | ~333ms |
| Minimum delay between writes | 180ms |
| Minimum page interval (practical) | 3–5 seconds |

> Scrolling character-by-character is **not viable** at 1200 baud — use page-based rotation instead.

### Brightness

The A125 board has **no brightness/contrast control** — only backlight on/off. Tested commands `0x5F`, `0x5D`, `0x5C`, `0x5B` with various values — none affected brightness. Dim backlight is due to LED/CCFL aging.

---

## saturn-lcd Daemon

A Python daemon that drives the LCD with auto-rotating system stats and interactive disk detail via the front buttons.

### Install

Requires Python 3 and `smartmontools` (`apt install smartmontools`).

```bash
cp saturn-lcd /usr/local/bin/saturn-lcd
chmod +x /usr/local/bin/saturn-lcd
cp saturn-lcd.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable saturn-lcd
systemctl start saturn-lcd
```

### Features

**Auto-rotate mode** — 7 screens, 5 seconds each:

| Screen | Line 1 | Line 2 |
|--------|--------|--------|
| 1 | Hostname | IP address |
| 2 | CPU temp + load avg | RAM used/total |
| 3 | Uptime | N disks online |
| 4 | CPU usage % | Date/time |
| 5 | Fan1 RPM | Fan2 RPM + duty % |
| 6 | Net Rx rate | Net Tx rate |
| 7 | CPU + Board temp | NIC temp |

Hardware info refreshes every 10 seconds. CPU usage is calculated as delta between readings (real-time, not since boot).

**Detail mode** — via buttons:

- **SELECT**: Enter detail mode → cycle through disks → return to auto after last disk
- **ENTER**: Cycle detail pages for current disk
- **Timeout**: Returns to auto mode after 15 seconds of inactivity

Detail pages per disk:

| Page | Content |
|------|---------|
| 1 | `sdX SIZE TEMP°C HEALTH` (+ `R:N!` if reallocated > 0) |
| 2 | Device model |
| 3 | Power-on hours, reallocated sectors, pending sectors |
| 4 | Power cycle count |

**Screen power management:**

- **Triple-click SELECT** (3 presses within 1.5s): Toggle LCD backlight off
- **10 minutes inactivity**: LCD turns off automatically
- **Any button press**: Wakes LCD from sleep (wake only, no action executed)

### Manual Control

```bash
systemctl stop saturn-lcd      # stop
systemctl start saturn-lcd     # start
systemctl restart saturn-lcd   # restart after editing script
journalctl -u saturn-lcd -f    # view logs
```

---

## Sensors

### Kernel Modules

```bash
modprobe coretemp
modprobe f71882fg
```

Add to `/etc/modules` for boot persistence:
```
coretemp
f71882fg
```

### Available Sensors

| Sensor | Source | Notes |
|--------|--------|-------|
| CPU Package + cores | `coretemp` | `/sys/class/hwmon/hwmonX/temp*_input` (millidegrees C) |
| Board temps (ACPI) | `acpitz` | 2 zones |
| NIC temp | `r8169` | |
| Voltages + thermistors | `f71869a` (Fintek) | 3.3V, 3VSB, Vbat, 3 thermal zones |

### Fintek F71869A

Detected at ISA 0xa20 with 3 fan channels and 3 PWM outputs. Runtime sysfs at:

```
/sys/devices/platform/f71882fg.2592/
```

Files: `fan{1,2,3}_input`, `pwm{1,2,3}`, `pwm{1,2,3}_enable`, `in{0-8}_input`, `temp{1-3}_input`

---

## Fan Control

The rear chassis fan is on **Fintek channel 2** (`fan2_input` / `pwm2`). Full PWM control confirmed working.

### PWM vs RPM

| PWM Value | RPM |
|-----------|-----|
| 255 (max) | ~1429 |
| 162 (auto) | ~940 |
| 128 | ~792 |
| 64 | ~377 |
| 30 (min) | 0 (stalls) |

> **Important**: Fan stalls below PWM ~60 and requires PWM 80+ to restart from standstill.

Channels 1 and 3 read 0 RPM (no fans connected on TS-470 Pro).

### Manual Control

```bash
P=/sys/devices/platform/f71882fg.2592

# Read current state
cat $P/fan2_input    # RPM
cat $P/pwm2         # duty cycle (0-255)
cat $P/pwm2_enable  # 1=manual, 2=auto

# Set manual mode and speed
echo 1 > $P/pwm2_enable
echo 200 > $P/pwm2

# Restore automatic
echo 2 > $P/pwm2_enable
```

### Automatic Fan Control (fancontrol)

Configuration at `/etc/fancontrol`:

```
INTERVAL=10
DEVPATH=hwmon3=devices/platform/f71882fg.2592
DEVNAME=hwmon3=f71869a
FCTEMPS=hwmon3/pwm2=hwmon2/temp1_input
FCFANS=hwmon3/pwm2=hwmon3/fan2_input
MINTEMP=hwmon3/pwm2=30
MAXTEMP=hwmon3/pwm2=70
MINSTART=hwmon3/pwm2=80
MINSTOP=hwmon3/pwm2=60
MINPWM=hwmon3/pwm2=60
MAXPWM=hwmon3/pwm2=255
```

- Temperature range: 30–70°C (CPU package)
- PWM range: 60–255 (never below 60 to prevent stall)
- Kick-start at PWM 80 when fan needs to spin up from standstill
- Check interval: 10 seconds

```bash
systemctl enable fancontrol
systemctl start fancontrol
```

---

## Compatible Models

The LCD protocol and sensor layout likely applies to other MAHOBAY-based QNAP models:

- TS-470, TS-470 Pro
- TS-670, TS-670 Pro
- TS-870, TS-870 Pro
- TS-470U-RP

If you have one of these and want to contribute findings, PRs are welcome.

---

## Notes

- The USB Copy front panel button is not accessible via the A125 serial protocol or Linux input subsystem. It likely requires the IT8528E EC.
- The A125 LCD backlight has no adjustable brightness — only on/off. Dim appearance is normal for aged units.
- Fan channels 1 and 3 have no fans connected on the TS-470 Pro (0 RPM).

---

## References

- [QNAP TS-453 Pro LCD/LEDs/fan/buttons (gist)](https://gist.github.com/zopieux/0b38fe1c3cd49039c98d5612ca84a045)
- [LCDProc icp_a106 driver](https://github.com/lcdproc/lcdproc/blob/master/server/drivers/icp_a106.c)
- [qnap8528 kernel module](https://github.com/0xGiddi/qnap8528)
- [QNAP-EC](https://github.com/Stonyx/QNAP-EC)
- [Unraid LCD_Manager plugin](https://forums.unraid.net/topic/136952-plugin-lcd_manager/)
- [Unraid QNAP-EC plugin](https://github.com/ich777/unraid-qnapec)
