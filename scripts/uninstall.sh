#!/usr/bin/env bash
#
# qnap-tsx70 uninstaller.
#
# Stops, disables and removes the project's services and binaries. Configuration
# and calibration state are KEPT by default so a reinstall picks them up again;
# pass --purge to remove them too.

set -euo pipefail

BIN_DIR="/usr/local/bin"
UNIT_DIR="/etc/systemd/system"
CONF_FILE="/etc/qnap-tsx70-lcd.conf"
STATE_DIR="/var/lib/qnap-tsx70"
MODULES_FILE="/etc/modules-load.d/qnap-tsx70.conf"

UNITS=(qnap-tsx70-lcd.service qnap-tsx70-fancontrol.service)
BINS=(qnap-tsx70-lcd qnap-tsx70-fancontrol)

PURGE=0
DRY_RUN=0
KEEP_MODULES=0

RED=""; YELLOW=""; GREEN=""; BOLD=""; RESET=""
if [ -t 1 ]; then
    RED=$'\033[31m'; YELLOW=$'\033[33m'; GREEN=$'\033[32m'
    BOLD=$'\033[1m'; RESET=$'\033[0m'
fi

info() { printf '%s\n' "$*"; }
ok()   { printf '%sok%s   %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%swarn%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/uninstall.sh [options]

Options:
  --purge          Also remove the configuration file and calibration state.
  --keep-modules   Keep /etc/modules-load.d/qnap-tsx70.conf.
  --dry-run        Print the actions without changing anything.
  -h, --help       Show this help.

Kept by default:
  /etc/qnap-tsx70-lcd.conf              your configuration
  /var/lib/qnap-tsx70/                  fan calibration results
  /var/backups/qnap-tsx70/              migration backups
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1 ;;
        --keep-modules) KEEP_MODULES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    die "this uninstaller must run as root (try: sudo $0)"
fi

step "Stopping services"
for unit in "${UNITS[@]}"; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1 || [ -f "$UNIT_DIR/$unit" ]; then
        # Fan control restores the chip's automatic mode on SIGTERM, so a clean
        # stop must happen before the binary is removed.
        run systemctl stop "$unit" >/dev/null 2>&1 || warn "could not stop $unit"
        run systemctl disable "$unit" >/dev/null 2>&1 || true
        ok "stopped and disabled $unit"
    fi
done

step "Clearing the display"
if [ -x "$BIN_DIR/qnap-tsx70-lcd" ] && [ "$DRY_RUN" -eq 0 ]; then
    if "$BIN_DIR/qnap-tsx70-lcd" --clear >/dev/null 2>&1; then
        ok "display cleared and backlight off"
    else
        warn "could not clear the display (panel may keep the last text)"
    fi
else
    info "       [dry-run] clear display"
fi

step "Removing files"
for unit in "${UNITS[@]}"; do
    if [ -f "$UNIT_DIR/$unit" ]; then
        run rm -f "$UNIT_DIR/$unit"
        ok "removed $UNIT_DIR/$unit"
    fi
done
for bin in "${BINS[@]}"; do
    if [ -f "$BIN_DIR/$bin" ]; then
        run rm -f "$BIN_DIR/$bin"
        ok "removed $BIN_DIR/$bin"
    fi
done
if [ "$KEEP_MODULES" -eq 0 ] && [ -f "$MODULES_FILE" ]; then
    run rm -f "$MODULES_FILE"
    ok "removed $MODULES_FILE"
fi
run systemctl daemon-reload

if [ "$PURGE" -eq 1 ]; then
    step "Purging configuration and state"
    [ -f "$CONF_FILE" ] && { run rm -f "$CONF_FILE"; ok "removed $CONF_FILE"; }
    [ -d "$STATE_DIR" ] && { run rm -rf "$STATE_DIR"; ok "removed $STATE_DIR"; }
    info "Migration backups under /var/backups/qnap-tsx70 were NOT removed."
else
    step "Kept"
    [ -f "$CONF_FILE" ] && info "  $CONF_FILE"
    [ -d "$STATE_DIR" ] && info "  $STATE_DIR"
    info "Use --purge to remove these as well."
fi

step "Done"
info "The fan controller is back under its own automatic mode."
info "Kernel modules coretemp and f71882fg were left loaded; remove them with"
info "  modprobe -r f71882fg"
