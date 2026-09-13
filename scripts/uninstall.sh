#!/usr/bin/env bash
#
# qnap-tsx70 uninstaller.
#
# Stops, disables and removes the project's services and binaries. Configuration
# and calibration state are KEPT by default so a reinstall picks them up again;
# pass --purge to remove them too.
#
# Nothing is removed until the fan service is provably stopped. Deleting the
# unit and the binary out from under a live fan writer would leave a process
# driving PWM registers with no supervisor and no way for systemd to stop it.

set -euo pipefail

# ---------------------------------------------------------------------------
# Test seams - see the same block in install.sh. Unset, these are the real
# system paths and the real process table.
# ---------------------------------------------------------------------------
TEST_ROOT="${QNAP_TSX70_TEST_ROOT:-}"
PROC_DIR="${QNAP_TSX70_PROC_DIR:-/proc}"
STOP_TIMEOUT="${QNAP_TSX70_STOP_TIMEOUT:-20}"

BIN_DIR="$TEST_ROOT/usr/local/bin"
UNIT_DIR="$TEST_ROOT/etc/systemd/system"
CONF_FILE="$TEST_ROOT/etc/qnap-tsx70-lcd.conf"
STATE_DIR="$TEST_ROOT/var/lib/qnap-tsx70"
MODULES_FILE="$TEST_ROOT/etc/modules-load.d/qnap-tsx70.conf"

LCD_UNIT="qnap-tsx70-lcd.service"
FAN_UNIT="qnap-tsx70-fancontrol.service"
UNITS=("$LCD_UNIT" "$FAN_UNIT")
BINS=(qnap-tsx70-lcd qnap-tsx70-fancontrol)
FAN_BINS=(qnap-tsx70-fancontrol)
# Patterns, not names: /proc/<pid>/exe of a `#!/usr/bin/env python3` service
# resolves to python3.14, and an exact match would fail to recognise it.
INTERPRETER_GLOBS=('python' 'python[0-9]*' 'perl' 'perl[0-9]*' 'bash' 'sh'
                   'dash' 'ash' 'ksh' 'zsh')

PURGE=0
DRY_RUN=0
KEEP_MODULES=0
FAN_SAFE_STATE=0
FAN_WAS_PRESENT=0

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

The uninstaller refuses to remove anything while a fan-control process is
still running. That is deliberate: see docs/FAN_CONTROL.md.
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

# Like run, but discards a real invocation's output and failure. Redirecting
# `run` itself would hide the dry-run preview along with the output.
run_quiet() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] %s\n' "$*"
        return 0
    fi
    "$@" >/dev/null 2>&1 || true
}

if [ -n "$TEST_ROOT" ]; then
    [ "$TEST_ROOT" = "/" ] && die "QNAP_TSX70_TEST_ROOT must not be /"
    [ -d "$TEST_ROOT" ] || die "QNAP_TSX70_TEST_ROOT is not a directory: $TEST_ROOT"
    warn "TEST MODE: every path below is prefixed with $TEST_ROOT"
    warn "no system path is touched; unset QNAP_TSX70_TEST_ROOT for a real uninstall"
elif [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    die "this uninstaller must run as root (try: sudo $0)"
fi

# "activating" and "deactivating" exit nonzero from is-active but are not
# stopped, and removing files under either is the hazard this script exists
# to prevent.
unit_is_active() {
    local state
    state="$(systemctl is-active "$1" 2>/dev/null || true)"
    case "$state" in
        active|activating|reloading|refreshing|deactivating) return 0 ;;
        "") systemctl is-active "$1" >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}
unit_known()      { [ -f "$UNIT_DIR/$1" ] || systemctl list-unit-files "$1" >/dev/null 2>&1; }

# --- process identity, matching install.sh ---------------------------------
pid_is_alive() { [ -d "$PROC_DIR/$1" ]; }

in_list() {
    local needle="$1"; shift
    local item
    for item in "$@"; do
        [ "$item" = "$needle" ] && return 0
    done
    return 1
}

is_interpreter() {
    local base="${1##*/}" pattern
    for pattern in "${INTERPRETER_GLOBS[@]}"; do
        # shellcheck disable=SC2053  # the right-hand side is a glob on purpose
        [[ $base == $pattern ]] && return 0
    done
    return 1
}

# A read-only subcommand is not a fan writer; treating one as a writer would
# refuse a perfectly safe uninstall because someone left `status` open.
READ_ONLY_SUBCOMMANDS=(status validate-cache --version --help)

pid_is_read_only() {
    local token
    [ -r "$PROC_DIR/$1/cmdline" ] || return 1
    while IFS= read -r token; do
        in_list "$token" "${READ_ONLY_SUBCOMMANDS[@]}" && return 0
    done < <(tr '\0' '\n' < "$PROC_DIR/$1/cmdline" 2>/dev/null)
    return 1
}

# pid_may_be <pid> <name>... - liberal identity, deliberately so.
#
# The only question this script asks of a process is "might this still be
# driving the fans?". A false positive costs a refusal the operator can act
# on; a false negative deletes the binary out from under a live writer.
pid_may_be() {
    local pid="$1"; shift
    local names=("$@") comm="" exe="" token
    pid_is_alive "$pid" || return 1
    [ -r "$PROC_DIR/$pid/comm" ] && comm="$(tr -d '\n' < "$PROC_DIR/$pid/comm" 2>/dev/null)"
    exe="$(readlink -f "$PROC_DIR/$pid/exe" 2>/dev/null || true)"
    if [ -n "$comm" ] && in_list "$comm" "${names[@]}"; then return 0; fi
    if [ -n "$exe" ] && in_list "${exe##*/}" "${names[@]}"; then return 0; fi
    if [ -r "$PROC_DIR/$pid/cmdline" ]; then
        while IFS= read -r token; do
            [ -n "$token" ] || continue
            in_list "${token##*/}" "${names[@]}" && return 0
        done < <(tr '\0' '\n' < "$PROC_DIR/$pid/cmdline" 2>/dev/null)
    fi
    return 1
}

known_processes() {
    local entry pid
    for entry in "$PROC_DIR"/[0-9]*; do
        [ -d "$entry" ] || continue
        pid="${entry##*/}"
        if pid_may_be "$pid" "$@" && ! pid_is_read_only "$pid"; then
            printf '%s\n' "$pid"
        fi
    done
    return 0
}

# stop_unit_verified <unit> - nonzero unless systemd agrees it is inactive.
stop_unit_verified() {
    local unit="$1"
    unit_is_active "$unit" || { ok "$unit already inactive"; return 0; }
    if ! run systemctl stop "$unit"; then
        warn "systemctl stop $unit failed"
        return 1
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] verify %s is inactive\n' "$unit"
        return 0
    fi
    local waited=0
    while unit_is_active "$unit"; do
        if [ "$waited" -ge "$STOP_TIMEOUT" ]; then
            warn "$unit is still active ${STOP_TIMEOUT}s after stop"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 0
}

fan_abort() {
    warn "refusing to continue: a fan writer may still be running."
    warn "Nothing has been removed, so you can still recover with:"
    warn "  systemctl status $FAN_UNIT"
    warn "  systemctl stop   $FAN_UNIT"
    warn "  $BIN_DIR/qnap-tsx70-fancontrol safe-state"
    warn "Once no fan-control process remains, re-run this uninstaller."
    die "fan control could not be confirmed stopped"
}

step "Stopping services"

# Fan control first, and it has to be provably down before anything is
# deleted. Its own SIGTERM handler is what hands the fans back to the chip.
if unit_known "$FAN_UNIT" || [ -x "$BIN_DIR/qnap-tsx70-fancontrol" ]; then
    FAN_WAS_PRESENT=1
fi

if unit_known "$FAN_UNIT" || unit_is_active "$FAN_UNIT"; then
    stop_unit_verified "$FAN_UNIT" || fan_abort
    run_quiet systemctl disable "$FAN_UNIT"
    ok "stopped and disabled $FAN_UNIT"
fi

if [ "$FAN_WAS_PRESENT" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        info "       [dry-run] verify no fan-control process remains"
    else
        waited=0
        while true; do
            remaining="$(known_processes "${FAN_BINS[@]}" | tr '\n' ' ' | sed 's/ *$//')"
            [ -z "$remaining" ] && break
            if [ "$waited" -ge "$STOP_TIMEOUT" ]; then
                warn "fan-control processes still running: $remaining"
                fan_abort
            fi
            sleep 1
            waited=$((waited + 1))
        done
        ok "no fan-control process remains"
    fi

    # Only now, with no live writer to race, is it safe to ask the chip to
    # take the fans back explicitly. The unit's ExecStopPost normally does
    # this already; this covers a unit that was removed or never installed.
    if [ -x "$BIN_DIR/qnap-tsx70-fancontrol" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            info "       [dry-run] qnap-tsx70-fancontrol safe-state"
            FAN_SAFE_STATE=1
        elif "$BIN_DIR/qnap-tsx70-fancontrol" safe-state >/dev/null 2>&1; then
            FAN_SAFE_STATE=1
            ok "fans handed back to the controller's automatic mode"
        else
            warn "safe-state did not confirm automatic mode; check the fans by hand:"
            warn "  qnap-tsx70-fancontrol status"
        fi
    fi
fi

if unit_known "$LCD_UNIT" || unit_is_active "$LCD_UNIT"; then
    if ! stop_unit_verified "$LCD_UNIT"; then
        warn "nothing has been removed; stop it by hand and re-run"
        die "$LCD_UNIT could not be confirmed stopped"
    fi
    run_quiet systemctl disable "$LCD_UNIT"
    ok "stopped and disabled $LCD_UNIT"
fi

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
if [ "$FAN_WAS_PRESENT" -eq 0 ]; then
    info "Fan control was not installed; the chip was already in charge of the fans."
elif [ "$FAN_SAFE_STATE" -eq 1 ]; then
    info "The fan controller is back under its own automatic mode (verified)."
else
    warn "Automatic fan mode was NOT verified. No fan-control process is running,"
    warn "so nothing is driving the PWM registers, but check the chip yourself:"
    warn "  grep . /sys/devices/platform/f71882fg.*/pwm*_enable   # 2 = automatic"
fi
info "Kernel modules coretemp and f71882fg were left loaded; remove them with"
info "  modprobe -r f71882fg"
