#!/usr/bin/env bash
#
# qnap-tsx70 installer.
#
# Default install is LCD-only. Fan control is experimental and never installed,
# enabled or calibrated unless --with-fan-control is given explicitly.
#
# Everything is copied from the versioned files in this repository; no unit or
# configuration content is generated here, so the repository is the only source
# of truth.

# -E (errtrace) matters: without it bash does not inherit the ERR trap
# into shell functions, so a failure inside install_files() or
# enable_services() would abort without ever running rollback().
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"

BIN_DIR="/usr/local/bin"
UNIT_DIR="/etc/systemd/system"
CONF_FILE="/etc/qnap-tsx70-lcd.conf"
STATE_DIR="/var/lib/qnap-tsx70"
MODULES_FILE="/etc/modules-load.d/qnap-tsx70.conf"
BACKUP_ROOT="/var/backups/qnap-tsx70"

# Fan units first: stopping them lets their own handler hand the fans back
# to the controller before the rest of the migration touches anything.
LEGACY_UNITS=(saturn-fancontrol.service saturn-fan.service saturn-lcd.service)
LEGACY_BINS=(saturn-lcd saturn-fan-calibrate saturn-fancontrol saturn-fan)
LEGACY_CACHE="/etc/saturn-fan-cache.json"
LEGACY_PID="/run/saturn-fancontrol.pid"

WITH_FAN=0
DRY_RUN=0
MIGRATE=1
SKIP_DEPS=0
FORCE=0
BACKUP_DIR=""
ROLLBACK_NEEDED=0

RED=""; YELLOW=""; GREEN=""; BOLD=""; RESET=""
if [ -t 1 ]; then
    RED=$'\033[31m'; YELLOW=$'\033[33m'; GREEN=$'\033[32m'
    BOLD=$'\033[1m'; RESET=$'\033[0m'
fi

info()  { printf '%s\n' "$*"; }
ok()    { printf '%sok%s   %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '%swarn%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()   { printf '%serror%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
step()  { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/install.sh [options]

Options:
  --lcd                 Install the LCD monitor only. This is the default.
  --with-fan-control    Also install the EXPERIMENTAL fan control service.
                        The service is installed and enabled, but calibration
                        is never run automatically - see docs/FAN_CONTROL.md.
  --no-migrate          Do not touch a legacy saturn-* installation.
  --skip-deps           Do not install distribution packages.
  --force               Continue even if the serial port preflight fails.
  --dry-run             Print the actions without changing anything.
  -h, --help            Show this help.

Examples:
  sudo ./scripts/install.sh                      # LCD only (recommended)
  sudo ./scripts/install.sh --with-fan-control   # LCD + experimental fan control
  sudo ./scripts/install.sh --dry-run            # preview
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --lcd) WITH_FAN=0 ;;
        --with-fan-control) WITH_FAN=1 ;;
        --no-migrate) MIGRATE=0 ;;
        --skip-deps) SKIP_DEPS=1 ;;
        --force) FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

# run <command...> - the single mutation choke point, so --dry-run is honest.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

need_root() {
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    [ "$(id -u)" -eq 0 ] || die "this installer must run as root (try: sudo $0)"
}

have() { command -v "$1" >/dev/null 2>&1; }

unit_exists() { [ -f "$UNIT_DIR/$1" ] || systemctl list-unit-files "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Preflight - runs before any mutation and aborts the whole install on failure.
# ---------------------------------------------------------------------------
preflight() {
    step "Preflight"
    local failed=0

    local required=(
        "bin/qnap-tsx70-lcd"
        "config/qnap-tsx70-lcd.conf.example"
        "systemd/qnap-tsx70-lcd.service"
    )
    if [ "$WITH_FAN" -eq 1 ]; then
        required+=("bin/qnap-tsx70-fancontrol" "systemd/qnap-tsx70-fancontrol.service")
    fi
    local path
    for path in "${required[@]}"; do
        if [ -f "$REPO_ROOT/$path" ]; then
            ok "found $path"
        else
            warn "missing $path"
            failed=1
        fi
    done

    if have systemctl; then ok "systemd present"; else warn "systemctl not found"; failed=1; fi
    if have python3; then
        ok "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    else
        warn "python3 not found"
        failed=1
    fi

    if python3 "$REPO_ROOT/bin/qnap-tsx70-lcd" --version >/dev/null 2>&1; then
        ok "LCD binary runs"
        # The binary's own preflight is the single source of truth for hardware
        # readiness; --check is read-only and never writes to the display.
        info "       --- qnap-tsx70-lcd --check ---"
        python3 "$REPO_ROOT/bin/qnap-tsx70-lcd" --config "$CONF_FILE" --check \
            2>&1 | sed 's/^/       /' || true
        info "       --- end of check ---"
    else
        warn "LCD binary failed to start"
        failed=1
    fi

    # sed exits 2 when the file is absent, and pipefail would turn that into a
    # silent abort of the whole installer on a first-time install.
    local port=""
    if [ -r "$CONF_FILE" ]; then
        port="$(sed -n 's/^[[:space:]]*serial_port[[:space:]]*=[[:space:]]*//p' \
                "$CONF_FILE" | tail -1)" || port=""
    fi
    port="${port:-/dev/ttyS1}"
    if [ -c "$port" ]; then
        ok "serial port $port present"
        if have fuser && fuser "$port" >/dev/null 2>&1; then
            warn "$port is already open by another process - stop it first"
            [ "$FORCE" -eq 1 ] || failed=1
        fi
    else
        warn "serial port $port not found (is this a TS-x70 with the A125 panel?)"
        [ "$FORCE" -eq 1 ] || failed=1
    fi

    if [ "$WITH_FAN" -eq 1 ]; then
        local matches
        matches="$(compgen -G '/sys/devices/platform/f71882fg.*' | wc -l || true)"
        if [ "${matches:-0}" -eq 1 ]; then
            ok "fan controller found"
        elif [ "${matches:-0}" -eq 0 ]; then
            warn "no f71882fg device - load the module first (modprobe f71882fg)"
            [ "$FORCE" -eq 1 ] || failed=1
        else
            warn "$matches f71882fg devices found - fan control refuses ambiguity"
            failed=1
        fi
    fi

    if [ "$failed" -ne 0 ]; then
        die "preflight failed; nothing has been changed. Fix the items above, or re-run with --force if you understand the risk."
    fi
    ok "preflight passed"
}

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
install_deps() {
    [ "$SKIP_DEPS" -eq 0 ] || { info "skipping dependency install"; return 0; }
    step "Dependencies"
    local missing=()
    have smartctl || missing+=("smartmontools")
    have lsblk || missing+=("util-linux")
    if [ "${#missing[@]}" -eq 0 ]; then
        ok "smartmontools and util-linux present"
        return 0
    fi
    if have apt-get; then
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}" \
            || warn "package install failed; continuing (disk pages will show N/A)"
    else
        warn "install these packages manually: ${missing[*]}"
    fi
}

# ---------------------------------------------------------------------------
# Legacy saturn-* migration
# ---------------------------------------------------------------------------
detect_legacy() {
    local found=0 unit bin
    for unit in "${LEGACY_UNITS[@]}"; do
        [ -f "$UNIT_DIR/$unit" ] && found=1
    done
    for bin in "${LEGACY_BINS[@]}"; do
        [ -f "$BIN_DIR/$bin" ] && found=1
    done
    [ -f "$LEGACY_CACHE" ] && found=1
    [ "$found" -eq 1 ]
}

make_backup() {
    BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
    run mkdir -p "$BACKUP_DIR"
    local unit bin
    for unit in "${LEGACY_UNITS[@]}"; do
        if [ -f "$UNIT_DIR/$unit" ]; then
            run cp -a "$UNIT_DIR/$unit" "$BACKUP_DIR/"
            if systemctl is-enabled "$unit" >/dev/null 2>&1; then
                run sh -c "printf '%s\n' '$unit' >> '$BACKUP_DIR/enabled-units'"
            fi
        fi
    done
    for bin in "${LEGACY_BINS[@]}"; do
        [ -f "$BIN_DIR/$bin" ] && run cp -a "$BIN_DIR/$bin" "$BACKUP_DIR/"
    done
    [ -f "$LEGACY_CACHE" ] && run cp -a "$LEGACY_CACHE" "$BACKUP_DIR/"
    [ -f "$CONF_FILE" ] && run cp -a "$CONF_FILE" "$BACKUP_DIR/"
    ok "backup written to $BACKUP_DIR"
}

stop_legacy() {
    local unit
    for unit in "${LEGACY_UNITS[@]}"; do
        if systemctl is-active "$unit" >/dev/null 2>&1; then
            run systemctl stop "$unit" || warn "could not stop $unit"
            ok "stopped $unit"
        fi
    done
}

migrate_cache() {
    [ -f "$LEGACY_CACHE" ] || return 0
    run mkdir -p "$STATE_DIR"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] migrate %s -> %s/fan-calibration.json\n' \
            "$LEGACY_CACHE" "$STATE_DIR"
        return 0
    fi
    if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$LEGACY_CACHE" 2>/dev/null; then
        install -m 0644 "$LEGACY_CACHE" "$STATE_DIR/fan-calibration.json"
        ok "migrated calibration cache to $STATE_DIR/fan-calibration.json"
    else
        warn "legacy cache $LEGACY_CACHE is not valid JSON - not migrated"
    fi
}

remove_legacy() {
    step "Removing legacy saturn-* artifacts"
    local unit bin
    for unit in "${LEGACY_UNITS[@]}"; do
        if unit_exists "$unit"; then
            run systemctl disable "$unit" >/dev/null 2>&1 || true
            run rm -f "$UNIT_DIR/$unit"
            ok "removed $unit"
        fi
    done
    for bin in "${LEGACY_BINS[@]}"; do
        if [ -f "$BIN_DIR/$bin" ]; then
            run rm -f "$BIN_DIR/$bin"
            ok "removed $BIN_DIR/$bin"
        fi
    done
    [ -f "$LEGACY_CACHE" ] && { run rm -f "$LEGACY_CACHE"; ok "removed $LEGACY_CACHE"; }
    [ -f "$LEGACY_PID" ] && run rm -f "$LEGACY_PID"
    run systemctl daemon-reload
}

rollback() {
    [ "$ROLLBACK_NEEDED" -eq 1 ] || return 0
    [ -n "$BACKUP_DIR" ] || return 0
    # Best effort from here: this runs while something has already gone wrong,
    # so one failed restore must not stop the remaining ones. Under `set -e` a
    # single failing cp would abandon the rest of the rollback half done.
    set +e
    warn "rolling back to the previous installation"
    local unit
    for unit in "${LEGACY_UNITS[@]}"; do
        [ -f "$BACKUP_DIR/$unit" ] && cp -a "$BACKUP_DIR/$unit" "$UNIT_DIR/$unit"
    done
    local bin
    for bin in "${LEGACY_BINS[@]}"; do
        [ -f "$BACKUP_DIR/$bin" ] && cp -a "$BACKUP_DIR/$bin" "$BIN_DIR/$bin"
    done
    [ -f "$BACKUP_DIR/$(basename "$LEGACY_CACHE")" ] && \
        cp -a "$BACKUP_DIR/$(basename "$LEGACY_CACHE")" "$LEGACY_CACHE"
    systemctl stop qnap-tsx70-lcd.service >/dev/null 2>&1 || true
    systemctl disable qnap-tsx70-lcd.service >/dev/null 2>&1 || true
    systemctl stop qnap-tsx70-fancontrol.service >/dev/null 2>&1 || true
    systemctl disable qnap-tsx70-fancontrol.service >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/qnap-tsx70-lcd.service" "$UNIT_DIR/qnap-tsx70-fancontrol.service"
    systemctl daemon-reload || true
    if [ -f "$BACKUP_DIR/enabled-units" ]; then
        while read -r unit; do
            [ -n "$unit" ] || continue
            systemctl enable "$unit" >/dev/null 2>&1 || true
            systemctl start "$unit" >/dev/null 2>&1 || true
        done < "$BACKUP_DIR/enabled-units"
    fi
    warn "rollback complete; originals remain in $BACKUP_DIR"
    set -e
    return 0
}

on_error() {
    # Accept an explicit status: when this is reached through `if ! validate`,
    # $? is already 0 because the negation succeeded, and exiting 0 would
    # report a rolled-back install as a success.
    local code=${1:-$?}
    [ "$code" -ne 0 ] || code=1
    printf '\n%serror%s installation failed (exit %d)\n' "$RED" "$RESET" "$code" >&2
    rollback
    exit "$code"
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
install_files() {
    step "Installing files"
    run install -d -m 0755 "$STATE_DIR"
    run install -m 0755 "$REPO_ROOT/bin/qnap-tsx70-lcd" "$BIN_DIR/qnap-tsx70-lcd"
    ok "installed $BIN_DIR/qnap-tsx70-lcd"

    if [ -f "$CONF_FILE" ]; then
        ok "kept existing $CONF_FILE (see config/qnap-tsx70-lcd.conf.example for new keys)"
    else
        run install -m 0644 "$REPO_ROOT/config/qnap-tsx70-lcd.conf.example" "$CONF_FILE"
        ok "installed $CONF_FILE"
    fi

    run install -m 0644 "$REPO_ROOT/systemd/qnap-tsx70-lcd.service" \
        "$UNIT_DIR/qnap-tsx70-lcd.service"
    ok "installed qnap-tsx70-lcd.service"

    if [ "$WITH_FAN" -eq 1 ]; then
        run install -m 0755 "$REPO_ROOT/bin/qnap-tsx70-fancontrol" \
            "$BIN_DIR/qnap-tsx70-fancontrol"
        run install -m 0644 "$REPO_ROOT/systemd/qnap-tsx70-fancontrol.service" \
            "$UNIT_DIR/qnap-tsx70-fancontrol.service"
        run install -d -m 0755 "$(dirname "$MODULES_FILE")"
        run sh -c "printf 'coretemp\nf71882fg\n' > '$MODULES_FILE'"
        run modprobe f71882fg 2>/dev/null || warn "modprobe f71882fg failed"
        run modprobe coretemp 2>/dev/null || true
        ok "installed fan control (experimental)"
    fi
}

enable_services() {
    step "Enabling services"
    run systemctl daemon-reload
    run systemctl enable qnap-tsx70-lcd.service
    run systemctl restart qnap-tsx70-lcd.service
    ok "qnap-tsx70-lcd enabled and started"

    if [ "$WITH_FAN" -eq 1 ]; then
        run systemctl enable qnap-tsx70-fancontrol.service
        # Not started here: 'run' requires a calibration that only the operator
        # may trigger, because calibration deliberately stalls the fan.
        ok "qnap-tsx70-fancontrol enabled but NOT started"
    fi
}

validate() {
    step "Validation"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "       [dry-run] skipping runtime validation"
        return 0
    fi
    sleep 2
    if systemctl is-active --quiet qnap-tsx70-lcd.service; then
        ok "qnap-tsx70-lcd is active"
    else
        warn "qnap-tsx70-lcd is not active:"
        systemctl status --no-pager --lines=15 qnap-tsx70-lcd.service >&2 || true
        return 1
    fi
    "$BIN_DIR/qnap-tsx70-lcd" --config "$CONF_FILE" --check || \
        warn "preflight reported warnings; the service is running anyway"
    return 0
}

main() {
    need_root
    info "${BOLD}qnap-tsx70 installer${RESET}"
    info "repository: $REPO_ROOT"
    if [ "$WITH_FAN" -eq 1 ]; then
        info "mode:       LCD + fan control (experimental)"
    else
        info "mode:       LCD only"
    fi
    [ "$DRY_RUN" -eq 1 ] && info "dry run:    no changes will be made"

    preflight
    install_deps

    local legacy=0
    if [ "$MIGRATE" -eq 1 ] && detect_legacy; then
        legacy=1
        step "Migrating legacy saturn-* installation"
        make_backup
        # Armed as soon as a backup exists: stopping the legacy services is
        # already a change worth undoing if what follows fails.
        trap 'on_error $?' ERR
        ROLLBACK_NEEDED=1
        stop_legacy
        migrate_cache
    fi

    trap 'on_error $?' ERR
    ROLLBACK_NEEDED=$legacy
    install_files
    enable_services
    if ! validate; then
        on_error 1
    fi
    ROLLBACK_NEEDED=0
    trap - ERR

    if [ "$legacy" -eq 1 ]; then
        remove_legacy
    fi

    step "Done"
    info "LCD service:  systemctl status qnap-tsx70-lcd"
    info "LCD logs:     journalctl -u qnap-tsx70-lcd -f"
    info "Config:       $CONF_FILE"
    info "Guide:        docs/LCD_GUIDE.md"
    if [ "$WITH_FAN" -eq 1 ]; then
        info ""
        warn "Fan control is installed but NOT running."
        info "It needs a calibration run that deliberately stalls the fan."
        info "Read docs/FAN_CONTROL.md first, then:"
        info "  qnap-tsx70-fancontrol status"
        info "  qnap-tsx70-fancontrol calibrate --yes"
        info "  systemctl start qnap-tsx70-fancontrol"
    fi
    if [ "$legacy" -eq 1 ]; then
        info ""
        info "Legacy saturn-* artifacts were migrated and removed."
        info "Backup: $BACKUP_DIR (see docs/MIGRATION_FROM_SATURN.md)"
    fi
}

main "$@"
