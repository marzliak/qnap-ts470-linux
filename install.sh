#!/bin/bash
# Saturn Install Script
# Installs LCD monitor, fan control, and systemd services
# Run as root on the QNAP TS-470 Pro

set -e

echo "=== Saturn Installer ==="

# Dependencies
apt install -y lm-sensors smartmontools

# Kernel modules
grep -q f71882fg /etc/modules || echo f71882fg >> /etc/modules
grep -q coretemp /etc/modules || echo coretemp >> /etc/modules
modprobe f71882fg 2>/dev/null || true
modprobe coretemp 2>/dev/null || true

# Install scripts
cp saturn-lcd /usr/local/bin/saturn-lcd
cp saturn-fan-calibrate /usr/local/bin/saturn-fan-calibrate
chmod +x /usr/local/bin/saturn-lcd
chmod +x /usr/local/bin/saturn-fan-calibrate

# Systemd: fan control daemon (calibrates on first run, then controls temperature)
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

# Systemd: LCD service (after fan control)
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

# Enable and start
systemctl daemon-reload
systemctl enable saturn-fancontrol
systemctl enable saturn-lcd

echo ""
echo "=== Starting services ==="
systemctl start saturn-fancontrol
sleep 2
systemctl start saturn-lcd

echo ""
echo "=== Status ==="
systemctl is-active saturn-fancontrol saturn-lcd

echo ""
echo "=== Saturn installed successfully ==="
echo "Boot order: modprobe -> saturn-fancontrol (calibrate + control loop) -> saturn-lcd"
echo ""
echo "Useful commands:"
echo "  systemctl status saturn-fancontrol       # fan control status"
echo "  systemctl status saturn-lcd       # LCD status"
echo "  journalctl -u saturn-fancontrol -f       # fan control logs"
echo "  saturn-fan-calibrate --force      # force recalibration"
echo "  saturn-fan-calibrate --calibrate-only  # calibrate without daemon"
