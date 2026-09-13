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
#
# The install is transactional. Before anything is changed, the previous
# existence, content, mode and systemd enablement/activity of every path this
# script can touch is recorded in a manifest under the backup directory. Any
# failure - including one during legacy cleanup - restores that exact state.

# -E (errtrace) matters: without it bash does not inherit the ERR trap
# into shell functions, so a failure inside install_files() or
# enable_services() would abort without ever running rollback().
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"

# ---------------------------------------------------------------------------
# Test seams.
#
# Production defaults are the real system paths and commands. The regression
# suite needs to exercise migration, stop failures, PID identity and rollback
# without root and without hardware, so each of those is reachable through one
# documented override. Nothing here changes behaviour when the variables are
# unset, which is the only state a real install ever sees.
#
#   QNAP_TSX70_TEST_ROOT    prefix for every absolute path below
#   QNAP_TSX70_PROC_DIR     process table to read identities from
#   QNAP_TSX70_KILL         command used to signal a validated legacy process
#   QNAP_TSX70_SETTLE_SECS  pause before post-install validation
#   QNAP_TSX70_STOP_TIMEOUT bound on every "wait until it is really gone" loop
#
# systemctl, fuser, modprobe and python3 are resolved from PATH, so a test
# supplies fakes by putting them earlier in PATH.
# ---------------------------------------------------------------------------
TEST_ROOT="${QNAP_TSX70_TEST_ROOT:-}"
PROC_DIR="${QNAP_TSX70_PROC_DIR:-/proc}"
KILL_CMD="${QNAP_TSX70_KILL:-kill}"
SETTLE_SECS="${QNAP_TSX70_SETTLE_SECS:-2}"
STOP_TIMEOUT="${QNAP_TSX70_STOP_TIMEOUT:-20}"

BIN_DIR="$TEST_ROOT/usr/local/bin"
UNIT_DIR="$TEST_ROOT/etc/systemd/system"
CONF_FILE="$TEST_ROOT/etc/qnap-tsx70-lcd.conf"
STATE_DIR="$TEST_ROOT/var/lib/qnap-tsx70"
MODULES_FILE="$TEST_ROOT/etc/modules-load.d/qnap-tsx70.conf"
BACKUP_ROOT="$TEST_ROOT/var/backups/qnap-tsx70"
CACHE_FILE="$STATE_DIR/fan-calibration.json"

NEW_LCD_UNIT="qnap-tsx70-lcd.service"
NEW_FAN_UNIT="qnap-tsx70-fancontrol.service"

# Fan units first: stopping them lets their own handler hand the fans back
# to the controller before the rest of the migration touches anything.
LEGACY_FAN_UNITS=(saturn-fancontrol.service saturn-fan.service)
LEGACY_LCD_UNITS=(saturn-lcd.service)
LEGACY_UNITS=("${LEGACY_FAN_UNITS[@]}" "${LEGACY_LCD_UNITS[@]}")
LEGACY_FAN_BINS=(saturn-fancontrol saturn-fan saturn-fan-calibrate)
LEGACY_LCD_BINS=(saturn-lcd)
LEGACY_BINS=("${LEGACY_LCD_BINS[@]}" "${LEGACY_FAN_BINS[@]}")
LEGACY_CACHE="$TEST_ROOT/etc/saturn-fan-cache.json"
LEGACY_PID="$TEST_ROOT/run/saturn-fancontrol.pid"

# Executables that may legitimately hold the panel's serial port.
LCD_OWNER_BINS=(qnap-tsx70-lcd "${LEGACY_LCD_BINS[@]}")
# Executables that may legitimately be driving the fans.
FAN_OWNER_BINS=(qnap-tsx70-fancontrol "${LEGACY_FAN_BINS[@]}")
# An interpreter is only ever accepted together with a script path in argv.
# Matched as a pattern: a script started through `#!/usr/bin/env python3` has
# comm=python3 but /proc/<pid>/exe resolving to /usr/bin/python3.14, and an
# exact-name test silently fails to recognise this project's own daemons.
INTERPRETER_GLOBS=('python' 'python[0-9]*' 'perl' 'perl[0-9]*' 'bash' 'sh'
                   'dash' 'ash' 'ksh' 'zsh')

WITH_FAN=0
DRY_RUN=0
MIGRATE=1
SKIP_DEPS=0
FORCE=0
ALLOW_SERIAL_OWNER=""
BACKUP_DIR=""
TXN_DIR=""
TXN_SEQ=0
TXN_PATHS=()
TXN_UNITS=()
TXN_DIRS=()
ROLLBACK_NEEDED=0
ROLLBACK_RUNNING=0
CACHE_MIGRATED=0
CACHE_PRESERVED=0

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
                        The unit is installed but only enabled when a valid
                        calibration cache already exists - see
                        docs/FAN_CONTROL.md.
  --no-migrate          Do not touch a legacy saturn-* installation.
  --skip-deps           Do not install distribution packages.
  --force               Continue when the serial port or the fan controller
                        is absent. It does NOT allow installing while an
                        unrecognised process holds the serial port.
  --allow-serial-owner PID
                        Accept PID as the current holder of the serial port
                        even though it is not one of this project's services.
                        The installer still never signals it, and still
                        refuses to continue unless the port is actually free
                        by the time the service has to open it.
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
        --allow-serial-owner)
            [ $# -ge 2 ] || { usage >&2; die "--allow-serial-owner needs a PID"; }
            ALLOW_SERIAL_OWNER="$2"
            shift ;;
        --allow-serial-owner=*) ALLOW_SERIAL_OWNER="${1#*=}" ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

if [ -n "$ALLOW_SERIAL_OWNER" ] && ! printf '%s' "$ALLOW_SERIAL_OWNER" | grep -qE '^[0-9]+$'; then
    die "--allow-serial-owner expects a numeric PID, got: $ALLOW_SERIAL_OWNER"
fi

# run <command...> - the single mutation choke point, so --dry-run is honest.
# Every path that changes the system goes through here, rollback included.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

# run_quiet <command...> - like run, but a real invocation's output and its
# failure are both discarded. The dry-run line is still printed; redirecting
# `run` itself would hide the preview along with the output.
run_quiet() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] %s\n' "$*"
        return 0
    fi
    "$@" >/dev/null 2>&1 || true
}

need_root() {
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    if [ -n "$TEST_ROOT" ]; then
        [ "$TEST_ROOT" = "/" ] && die "QNAP_TSX70_TEST_ROOT must not be /"
        [ -d "$TEST_ROOT" ] || die "QNAP_TSX70_TEST_ROOT is not a directory: $TEST_ROOT"
        warn "TEST MODE: every path below is prefixed with $TEST_ROOT"
        warn "no system path is touched; unset QNAP_TSX70_TEST_ROOT for a real install"
        return 0
    fi
    [ "$(id -u)" -eq 0 ] || die "this installer must run as root (try: sudo $0)"
}

have() { command -v "$1" >/dev/null 2>&1; }

unit_exists() { [ -f "$UNIT_DIR/$1" ] || systemctl list-unit-files "$1" >/dev/null 2>&1; }

# `systemctl is-active` exits nonzero for "activating" and "deactivating".
# Treating either as stopped would let the installer remove files while the
# unit is on its way up.
unit_is_active() {
    local state
    state="$(systemctl is-active "$1" 2>/dev/null || true)"
    case "$state" in
        active|activating|reloading|refreshing|deactivating) return 0 ;;
        "") systemctl is-active "$1" >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}
unit_is_enabled() { systemctl is-enabled "$1" >/dev/null 2>&1; }

# Under a test root nothing can be a real device node, so the type check is
# relaxed to "the path exists". Unset the variable and this is `test -c` again.
port_present() {
    if [ -n "$TEST_ROOT" ]; then [ -e "$1" ]; else [ -c "$1" ]; fi
}

# ---------------------------------------------------------------------------
# Process identity
#
# Nothing is ever signalled, and no port owner is ever accepted, on the
# strength of a name that merely appears somewhere in a command line. The
# identity has to come from comm, from the resolved executable, or from an
# interpreter running an absolute path whose basename is one of ours.
# ---------------------------------------------------------------------------
pid_is_alive() { [ -d "$PROC_DIR/$1" ]; }

pid_comm() {
    [ -r "$PROC_DIR/$1/comm" ] || return 1
    tr -d '\n' < "$PROC_DIR/$1/comm" 2>/dev/null
}

pid_exe() {
    local target
    target="$(readlink -f "$PROC_DIR/$1/exe" 2>/dev/null || true)"
    [ -n "$target" ] || return 1
    printf '%s' "$target"
}

pid_argv() {
    [ -r "$PROC_DIR/$1/cmdline" ] || return 1
    tr '\0' '\n' < "$PROC_DIR/$1/cmdline" 2>/dev/null
}

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

# pid_is_definitely <pid> <name>... - strict identity.
#
# Used wherever being wrong means acting on a stranger: signalling a process,
# or accepting one as the rightful owner of the serial port. A name that
# merely appears in a command line is not enough; it has to be comm, the
# resolved executable, or an interpreter running an absolute path of ours.
pid_is_definitely() {
    local pid="$1"; shift
    local names=("$@")
    pid_is_alive "$pid" || return 1

    local comm exe token
    comm="$(pid_comm "$pid" || true)"
    if [ -n "$comm" ] && in_list "$comm" "${names[@]}"; then
        return 0
    fi
    exe="$(pid_exe "$pid" || true)"
    if [ -n "$exe" ] && in_list "${exe##*/}" "${names[@]}"; then
        return 0
    fi
    if { [ -n "$exe" ] && is_interpreter "$exe"; } \
       || { [ -n "$comm" ] && is_interpreter "$comm"; }; then
        while IFS= read -r token; do
            case "$token" in
                /*) in_list "${token##*/}" "${names[@]}" && return 0 ;;
            esac
        done < <(pid_argv "$pid" || true)
    fi
    return 1
}

# Subcommands that only read. A `qnap-tsx70-fancontrol status` in another
# terminal, or this installer's own validate-cache child, is not a writer, and
# treating it as one would stall every run for STOP_TIMEOUT and then abort.
READ_ONLY_SUBCOMMANDS=(status validate-cache --version --help)

pid_is_read_only() {
    local token
    while IFS= read -r token; do
        in_list "$token" "${READ_ONLY_SUBCOMMANDS[@]}" && return 0
    done < <(pid_argv "$1" || true)
    return 1
}

# pid_may_be <pid> <name>... - liberal identity.
#
# Used only to answer "might a fan writer still be alive?". The costs are not
# symmetric there: a false positive stops the installer with a message, a
# false negative deletes the binary out from under a process driving PWM.
pid_may_be() {
    local pid="$1"; shift
    local names=("$@") token
    pid_is_definitely "$pid" "$@" && return 0
    while IFS= read -r token; do
        [ -n "$token" ] || continue
        in_list "${token##*/}" "${names[@]}" && return 0
    done < <(pid_argv "$pid" || true)
    return 1
}

pid_describe() {
    local pid="$1" comm exe
    comm="$(pid_comm "$pid" || true)"
    exe="$(pid_exe "$pid" || true)"
    printf 'pid %s (comm=%s exe=%s)' "$pid" "${comm:-?}" "${exe:-?}"
}

# known_processes <name>... - PIDs that might be one of those executables.
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

# True when this host can actually be asked who holds a device.
can_inspect_port() { have fuser || have lsof; }

port_holders() {
    local port="$1" raw token digits
    if have fuser; then
        raw="$(fuser "$port" 2>/dev/null || true)"
    elif have lsof; then
        raw="$(lsof -t -- "$port" 2>/dev/null || true)"
    else
        return 0
    fi
    for token in $raw; do
        digits="$(printf '%s' "$token" | tr -cd '0-9')"
        [ -n "$digits" ] && printf '%s\n' "$digits"
    done
    return 0
}

unit_main_pid() {
    local pid
    pid="$(systemctl show -p MainPID --value "$1" 2>/dev/null || true)"
    printf '%s' "${pid//[^0-9]/}"
}

# ---------------------------------------------------------------------------
# Effective configuration
#
# The Python binary owns the config grammar - quoting, inline comments and
# duplicate keys included. Re-implementing any of that in shell is what made
# the installer and the runtime disagree about a perfectly valid serial_port,
# so both scripts ask the binary instead.
# ---------------------------------------------------------------------------
config_value() {
    local key="$1" value
    # The repository copy answers, not the installed one: a reinstall over an
    # older version must not depend on that version already knowing the flag.
    if ! value="$(python3 "$REPO_ROOT/bin/qnap-tsx70-lcd" --config "$CONF_FILE" \
                    --print-config "$key" 2>/dev/null)"; then
        return 1
    fi
    printf '%s' "$value"
}

configured_port() {
    local port
    if port="$(config_value serial_port)" && [ -n "$port" ]; then
        printf '%s' "$port"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Preflight - runs before any mutation and aborts the whole install on failure.
# ---------------------------------------------------------------------------
SERIAL_PORT=""
SERIAL_OWNER_KIND="free"
SERIAL_OWNER_PIDS=""
SERIAL_OWNER_OVERRIDDEN=0

classify_serial_owner() {
    local port="$1" pids pid unit
    pids="$(port_holders "$port")"
    SERIAL_OWNER_PIDS="$(printf '%s' "$pids" | tr '\n' ' ' | sed 's/ *$//')"
    if [ -z "$pids" ]; then
        SERIAL_OWNER_KIND="free"
        return 0
    fi
    local all_known=1
    for pid in $pids; do
        if [ -n "$ALLOW_SERIAL_OWNER" ] && [ "$pid" = "$ALLOW_SERIAL_OWNER" ]; then
            SERIAL_OWNER_OVERRIDDEN=1
            continue
        fi
        if pid_is_definitely "$pid" "${LCD_OWNER_BINS[@]}"; then
            continue
        fi
        # systemd knows which process belongs to which unit even when the
        # executable has already been replaced on disk.
        local matched=0
        for unit in "$NEW_LCD_UNIT" "${LEGACY_LCD_UNITS[@]}"; do
            [ "$(unit_main_pid "$unit")" = "$pid" ] && { matched=1; break; }
        done
        [ "$matched" -eq 1 ] && continue
        all_known=0
        warn "$port is held by an unrecognised process: $(pid_describe "$pid")"
    done
    if [ "$all_known" -eq 1 ]; then
        SERIAL_OWNER_KIND="known"
    else
        SERIAL_OWNER_KIND="unknown"
    fi
}

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

    if ! SERIAL_PORT="$(configured_port)"; then
        warn "cannot read serial_port from $CONF_FILE - fix the configuration"
        failed=1
        SERIAL_PORT="/dev/ttyS1"
    fi
    if port_present "$SERIAL_PORT"; then
        ok "serial port $SERIAL_PORT present"
        if ! can_inspect_port; then
            warn "neither fuser nor lsof is installed, so who holds"
            warn "$SERIAL_PORT cannot be determined. Install psmisc, or re-run"
            warn "with --force if you have checked the port yourself."
            [ "$FORCE" -eq 1 ] || failed=1
        fi
        classify_serial_owner "$SERIAL_PORT"
        case "$SERIAL_OWNER_KIND" in
            free) ok "serial port is free" ;;
            known)
                if [ "$SERIAL_OWNER_OVERRIDDEN" -eq 1 ]; then
                    ok "serial port owner accepted (pids: $SERIAL_OWNER_PIDS)"
                else
                    ok "serial port held by this project's own service (pids: $SERIAL_OWNER_PIDS)"
                    info "       it will be stopped as part of the planned transition"
                fi
                ;;
            unknown)
                warn "$SERIAL_PORT is held by a process this installer does not recognise"
                warn "stop it yourself, or re-run with --allow-serial-owner PID once you"
                warn "have identified it. --force deliberately does not cover this case."
                failed=1
                ;;
        esac
        if [ -n "$ALLOW_SERIAL_OWNER" ]; then
            warn "accepting pid $ALLOW_SERIAL_OWNER as the serial port owner on your word;"
            warn "it will not be signalled, and the port must be free before the service starts"
        fi
    else
        warn "serial port $SERIAL_PORT not found (is this a TS-x70 with the A125 panel?)"
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
# Transaction manifest
#
# Rollback cannot guess what was there before. Every path and unit that this
# run may change is recorded first: whether it existed, its mode, a byte copy
# of its content, and whether systemd had it enabled and active. Restoration
# replays exactly that, so a path that already existed is put back instead of
# being deleted, and a path this run created is removed.
# ---------------------------------------------------------------------------
txn_begin() {
    BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-$$"
    TXN_DIR="$BACKUP_DIR/txn"
    txn_mkdir "$BACKUP_ROOT"
    run mkdir -p "$TXN_DIR/files"
    if [ "$DRY_RUN" -eq 0 ]; then
        : > "$TXN_DIR/manifest.tsv"
        : > "$TXN_DIR/units.tsv"
        chmod 0700 "$BACKUP_DIR" 2>/dev/null || true
    fi
    ok "transaction manifest in $TXN_DIR"
}

# txn_mkdir <dir> - create a directory, remembering every level it had to
# create. `mkdir -p /var/lib/qnap-tsx70` can invent /var and /var/lib too, and
# a rollback that only knows about the leaf leaves those behind.
txn_mkdir() {
    local dir="$1" mode="${2:-}"
    # An existing directory keeps the mode it already has. `install -d -m 0755`
    # would silently re-mode a system directory the installer does not own.
    [ -d "$dir" ] && return 0
    local probe="$dir"
    local missing=()
    while [ -n "$probe" ] && [ "$probe" != "/" ] && [ ! -d "$probe" ]; do
        missing=("$probe" ${missing[@]+"${missing[@]}"})
        probe="$(dirname "$probe")"
    done
    run mkdir -p "$dir"
    [ -n "$mode" ] && run chmod "$mode" "$dir"
    TXN_DIRS+=(${missing[@]+"${missing[@]}"})
    return 0
}

txn_record_path() {
    local path="$1" existed=0 mode="" blob="-"
    if [ -e "$path" ]; then
        existed=1
        mode="$(stat -c '%a' "$path" 2>/dev/null || echo 644)"
    fi
    TXN_PATHS+=("$path")
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] record %s (%s)\n' "$path" \
            "$([ "$existed" -eq 1 ] && echo present || echo absent)"
        return 0
    fi
    if [ "$existed" -eq 1 ] && [ -f "$path" ]; then
        TXN_SEQ=$((TXN_SEQ + 1))
        blob="$(printf 'f%04d' "$TXN_SEQ")"
        cp -a "$path" "$TXN_DIR/files/$blob"
    fi
    printf '%s\t%s\t%s\t%s\n' "$path" "$existed" "${mode:-644}" "$blob" \
        >> "$TXN_DIR/manifest.tsv"
}

txn_record_unit() {
    local unit="$1" enabled=0 active=0
    unit_is_enabled "$unit" && enabled=1
    unit_is_active "$unit" && active=1
    TXN_UNITS+=("$unit")
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] record unit %s (enabled=%s active=%s)\n' \
            "$unit" "$enabled" "$active"
        return 0
    fi
    printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >> "$TXN_DIR/units.tsv"
}

# Record everything before the first change, whether or not a legacy install
# is present: a plain reinstall replaces the same paths a migration does.
txn_snapshot_all() {
    local unit bin
    txn_record_path "$BIN_DIR/qnap-tsx70-lcd"
    txn_record_path "$BIN_DIR/qnap-tsx70-fancontrol"
    txn_record_path "$CONF_FILE"
    txn_record_path "$UNIT_DIR/$NEW_LCD_UNIT"
    txn_record_path "$UNIT_DIR/$NEW_FAN_UNIT"
    txn_record_path "$MODULES_FILE"
    txn_record_path "$CACHE_FILE"
    for unit in "${LEGACY_UNITS[@]}"; do
        txn_record_path "$UNIT_DIR/$unit"
    done
    for bin in "${LEGACY_BINS[@]}"; do
        txn_record_path "$BIN_DIR/$bin"
    done
    txn_record_path "$LEGACY_CACHE"
    txn_record_path "$LEGACY_PID"

    txn_record_unit "$NEW_LCD_UNIT"
    txn_record_unit "$NEW_FAN_UNIT"
    for unit in "${LEGACY_UNITS[@]}"; do
        txn_record_unit "$unit"
    done
    ok "recorded ${#TXN_PATHS[@]} paths and ${#TXN_UNITS[@]} units"
}

# The human-readable copy the migration document tells people to use. Laid out
# by kind so a restore command can name binaries without also matching units
# or the cache.
make_backup() {
    local unit bin
    run mkdir -p "$BACKUP_DIR/bin" "$BACKUP_DIR/systemd" "$BACKUP_DIR/state" \
        "$BACKUP_DIR/config"
    for unit in "${LEGACY_UNITS[@]}"; do
        if [ -f "$UNIT_DIR/$unit" ]; then
            run cp -a "$UNIT_DIR/$unit" "$BACKUP_DIR/systemd/"
            if unit_is_enabled "$unit"; then
                run sh -c "printf '%s\n' '$unit' >> '$BACKUP_DIR/enabled-units'"
            fi
        fi
    done
    for bin in "${LEGACY_BINS[@]}"; do
        [ -f "$BIN_DIR/$bin" ] && run cp -a "$BIN_DIR/$bin" "$BACKUP_DIR/bin/"
    done
    [ -f "$LEGACY_CACHE" ] && run cp -a "$LEGACY_CACHE" "$BACKUP_DIR/state/"
    [ -f "$CONF_FILE" ] && run cp -a "$CONF_FILE" "$BACKUP_DIR/config/"
    ok "backup written to $BACKUP_DIR"
}

# ---------------------------------------------------------------------------
# Stopping things, and proving they stopped
# ---------------------------------------------------------------------------

# stop_unit_verified <unit> - stop it and refuse to return until systemd
# agrees it is inactive. Returns nonzero instead of warning: every caller
# removes files afterwards, and a live writer must stop that.
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
        [ "$waited" -ge "$STOP_TIMEOUT" ] && {
            warn "$unit is still active ${STOP_TIMEOUT}s after stop"
            return 1
        }
        sleep 1
        waited=$((waited + 1))
    done
    ok "stopped $unit"
    return 0
}

# wait_for_no_process <label> <name>... - bounded wait until no live process
# matches one of the known executables.
wait_for_no_process() {
    local label="$1"; shift
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] verify no %s process remains\n' "$label"
        return 0
    fi
    local waited=0 remaining
    while true; do
        remaining="$(known_processes "$@" | tr '\n' ' ' | sed 's/ *$//')"
        [ -z "$remaining" ] && break
        if [ "$waited" -ge "$STOP_TIMEOUT" ]; then
            warn "$label still running after ${STOP_TIMEOUT}s: $remaining"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    ok "no $label process remains"
    return 0
}

fan_recovery_hint() {
    warn "nothing has been removed. Recover manually:"
    warn "  systemctl status ${LEGACY_FAN_UNITS[*]} $NEW_FAN_UNIT"
    warn "  systemctl stop   ${LEGACY_FAN_UNITS[*]} $NEW_FAN_UNIT"
    warn "  qnap-tsx70-fancontrol safe-state    # hands the fans back to the chip"
    warn "then re-run this installer."
}

# stop_fan_services - every fan writer, old and new, off and proven off.
# Units first, then the bare legacy daemon named by its PID file, and only
# then the quiescence gate: the PID-file process is a fan writer nothing else
# supervises, so checking for it before asking it to stop would always abort.
stop_fan_services() {
    local unit
    for unit in "${LEGACY_FAN_UNITS[@]}" "$NEW_FAN_UNIT"; do
        unit_exists "$unit" || unit_is_active "$unit" || continue
        if ! stop_unit_verified "$unit"; then
            fan_recovery_hint
            return 1
        fi
    done
    if ! stop_legacy_pid_process; then
        fan_recovery_hint
        return 1
    fi
    if ! wait_for_no_process "fan control" "${FAN_OWNER_BINS[@]}"; then
        fan_recovery_hint
        return 1
    fi
    return 0
}

# stop_legacy_pid_process - the PID file names a process that may still be
# driving the fans. It is validated before anything is signalled, and the file
# is never deleted on a guess.
stop_legacy_pid_process() {
    [ -e "$LEGACY_PID" ] || return 0
    if [ ! -f "$LEGACY_PID" ]; then
        warn "$LEGACY_PID exists but is not a regular file - refusing to guess"
        return 1
    fi
    local raw pid
    raw="$(head -c 64 "$LEGACY_PID" 2>/dev/null | head -1 | tr -d '[:space:]')" || raw=""
    if [ -z "$raw" ]; then
        # An empty file names nobody. That is only safe to ignore while no
        # legacy fan process is running; the quiescence gate below decides.
        if [ "$DRY_RUN" -eq 0 ] && known_processes "${LEGACY_FAN_BINS[@]}" | grep -q .; then
            warn "$LEGACY_PID is empty while a legacy fan process is running"
            warn "stop it by hand and re-run the installer"
            return 1
        fi
        ok "$LEGACY_PID is empty and no legacy fan process is running"
        return 0
    fi
    if ! printf '%s' "$raw" | grep -qE '^[0-9]+$'; then
        warn "$LEGACY_PID does not contain a PID (got: $raw)"
        warn "refusing to signal anything; inspect and remove it by hand"
        return 1
    fi
    pid="$raw"
    if [ "$pid" -le 1 ]; then
        warn "$LEGACY_PID names pid $pid, which cannot be a legacy fan process"
        return 1
    fi
    if ! pid_is_alive "$pid"; then
        ok "$LEGACY_PID is stale (pid $pid is gone)"
        return 0
    fi
    if ! pid_is_definitely "$pid" "${LEGACY_FAN_BINS[@]}"; then
        warn "$LEGACY_PID names a live process that is not a legacy fan"
        warn "controller: $(pid_describe "$pid")"
        warn "The PID was almost certainly reused. Nothing has been signalled."
        warn "Verify it, remove $LEGACY_PID by hand, then re-run the installer."
        return 1
    fi
    info "       legacy fan process still running: $(pid_describe "$pid")"
    # Re-checked immediately before the signal. This narrows, but cannot close,
    # the window in which the process could exit and the PID be reused; the
    # verification after the signal is what catches the rest.
    if ! pid_is_definitely "$pid" "${LEGACY_FAN_BINS[@]}"; then
        warn "pid $pid stopped being a legacy fan controller; nothing signalled"
        return 1
    fi
    if ! run "$KILL_CMD" -TERM "$pid"; then
        warn "could not signal pid $pid"
        return 1
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] wait for pid %s to exit\n' "$pid"
        return 0
    fi
    local waited=0
    while pid_is_alive "$pid"; do
        if [ "$waited" -ge "$STOP_TIMEOUT" ]; then
            warn "pid $pid ignored SIGTERM for ${STOP_TIMEOUT}s"
            warn "It is deliberately not killed: SIGKILL would skip whatever"
            warn "hands the fans back to the controller. Stop it yourself and"
            warn "re-run the installer."
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    ok "legacy fan process $pid exited"
    return 0
}

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

# stop_current_services - the planned transition. Fan writers first, then the
# LCD, so the serial port is free before anything reopens it.
stop_current_services() {
    step "Stopping services for the transition"
    stop_fan_services || return 1
    local unit
    for unit in "${LEGACY_LCD_UNITS[@]}" "$NEW_LCD_UNIT"; do
        unit_exists "$unit" || unit_is_active "$unit" || continue
        if ! stop_unit_verified "$unit"; then
            warn "nothing has been removed; start it again or stop it by hand,"
            warn "then re-run the installer"
            return 1
        fi
    done
    return 0
}

# verify_port_released - the port has to be genuinely free before the new
# service is asked to open it, whoever was holding it.
verify_port_released() {
    port_present "$SERIAL_PORT" || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] verify %s is released\n' "$SERIAL_PORT"
        return 0
    fi
    if ! can_inspect_port; then
        warn "cannot confirm $SERIAL_PORT is free without fuser or lsof"
        [ "$FORCE" -eq 1 ] || return 1
        warn "continuing because --force was given"
        return 0
    fi
    local waited=0 holders
    while true; do
        holders="$(port_holders "$SERIAL_PORT" | tr '\n' ' ' | sed 's/ *$//')"
        [ -z "$holders" ] && break
        if [ "$waited" -ge "$STOP_TIMEOUT" ]; then
            warn "$SERIAL_PORT is still held by: $holders"
            warn "the LCD service cannot open it; stop that process and re-run"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    ok "$SERIAL_PORT is free"
    return 0
}

migrate_cache() {
    [ -f "$LEGACY_CACHE" ] || return 0
    txn_mkdir "$STATE_DIR" 0755
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] migrate %s -> %s\n' "$LEGACY_CACHE" "$CACHE_FILE"
        return 0
    fi
    if cache_is_valid "$CACHE_FILE"; then
        CACHE_PRESERVED=1
        ok "kept the existing calibration at $CACHE_FILE"
        warn "the legacy cache was not migrated over it, so it is left at"
        warn "$LEGACY_CACHE as well"
    elif cache_is_valid "$LEGACY_CACHE"; then
        install -m 0644 "$LEGACY_CACHE" "$CACHE_FILE"
        CACHE_MIGRATED=1
        ok "migrated calibration cache to $CACHE_FILE"
    else
        CACHE_PRESERVED=1
        warn "legacy cache $LEGACY_CACHE did not validate - not migrated"
        warn "it is left exactly where it is, and a copy is in $BACKUP_DIR/state/"
    fi
}

# cache_is_valid <path> - the fan binary owns the calibration schema, so it
# decides. Read-only, touches no hardware.
cache_is_valid() {
    local path="$1" binary
    [ -f "$path" ] || return 1
    # The repository copy is the authority, as for the config: an installed
    # older binary may not know the current schema.
    binary="$REPO_ROOT/bin/qnap-tsx70-fancontrol"
    [ -f "$binary" ] || return 1
    python3 "$binary" validate-cache --cache "$path" >/dev/null 2>&1
}

remove_legacy() {
    step "Removing legacy saturn-* artifacts"
    # Re-checked here and not only before the install: this is the point where
    # the binary a fan writer is running gets deleted.
    if ! wait_for_no_process "fan control" "${FAN_OWNER_BINS[@]}"; then
        fan_recovery_hint
        return 1
    fi
    local unit bin
    for unit in "${LEGACY_UNITS[@]}"; do
        if unit_exists "$unit"; then
            run_quiet systemctl disable "$unit"
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
    if [ -f "$LEGACY_CACHE" ]; then
        if [ "$CACHE_MIGRATED" -eq 1 ] && cache_is_valid "$CACHE_FILE"; then
            run rm -f "$LEGACY_CACHE"
            ok "removed $LEGACY_CACHE (migrated and validated)"
        else
            CACHE_PRESERVED=1
            warn "kept $LEGACY_CACHE: it was not migrated, so it is the only"
            warn "copy of that calibration outside the backup"
        fi
    fi
    # The PID file goes only once the process it names is provably gone.
    if [ -f "$LEGACY_PID" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '       [dry-run] rm -f %s\n' "$LEGACY_PID"
        elif known_processes "${LEGACY_FAN_BINS[@]}" | grep -q .; then
            warn "kept $LEGACY_PID: a legacy fan process is still running"
            return 1
        else
            run rm -f "$LEGACY_PID"
            ok "removed $LEGACY_PID"
        fi
    fi
    run systemctl daemon-reload
    if [ "$CACHE_PRESERVED" -eq 1 ]; then
        info "       legacy artifacts removed except the calibration cache noted above"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
rollback() {
    [ "$ROLLBACK_NEEDED" -eq 1 ] || return 0
    [ "$ROLLBACK_RUNNING" -eq 0 ] || return 0
    ROLLBACK_RUNNING=1
    # `set +e` alone does not remove an inherited ERR trap, so a failing
    # restore would re-enter on_error and recurse. Disarm it explicitly.
    trap - ERR
    set +e

    warn "rolling back to the previous installation"
    local failures=0

    if [ "$DRY_RUN" -eq 1 ]; then
        local item
        info "       [dry-run] rollback would restore:"
        for item in ${TXN_PATHS[@]+"${TXN_PATHS[@]}"}; do
            info "       [dry-run]   path $item"
        done
        for item in ${TXN_UNITS[@]+"${TXN_UNITS[@]}"}; do
            info "       [dry-run]   unit $item"
        done
        warn "[dry-run] nothing was changed, so nothing was restored"
        set -e
        return 0
    fi

    if [ -z "$TXN_DIR" ] || [ ! -f "$TXN_DIR/manifest.tsv" ]; then
        warn "no transaction manifest; cannot restore automatically"
        set -e
        return 1
    fi

    local unit enabled active
    # 1. Undo enablement and activity this run introduced, before the files
    #    those units point at are replaced.
    while IFS=$'\t' read -r unit enabled active; do
        [ -n "$unit" ] || continue
        if [ "$active" -eq 0 ] && unit_is_active "$unit"; then
            systemctl stop "$unit" >/dev/null 2>&1 || failures=$((failures + 1))
        fi
        if [ "$enabled" -eq 0 ] && unit_is_enabled "$unit"; then
            systemctl disable "$unit" >/dev/null 2>&1 || failures=$((failures + 1))
        fi
    done < "$TXN_DIR/units.tsv"

    # 2. Put every recorded path back exactly as it was.
    local path existed mode blob
    while IFS=$'\t' read -r path existed mode blob; do
        [ -n "$path" ] || continue
        if [ "$existed" -eq 1 ]; then
            if [ "$blob" != "-" ] && [ -f "$TXN_DIR/files/$blob" ]; then
                mkdir -p "$(dirname "$path")" 2>/dev/null
                if install -m "$mode" "$TXN_DIR/files/$blob" "$path"; then
                    :
                else
                    warn "could not restore $path"
                    failures=$((failures + 1))
                fi
            fi
        elif [ -e "$path" ]; then
            if ! rm -f "$path"; then
                warn "could not remove $path"
                failures=$((failures + 1))
            fi
        fi
    done < "$TXN_DIR/manifest.tsv"

    # 3. Directories this run created and that are now empty.
    local dir index
    for (( index=${#TXN_DIRS[@]}-1 ; index>=0 ; index-- )); do
        dir="${TXN_DIRS[index]}"
        case "$dir" in
            "$BACKUP_ROOT"|"$BACKUP_DIR"|"$TXN_DIR") continue ;;
        esac
        rmdir "$dir" 2>/dev/null || true
    done

    systemctl daemon-reload >/dev/null 2>&1 || failures=$((failures + 1))

    # 4. Restore the enablement and activity that existed before.
    while IFS=$'\t' read -r unit enabled active; do
        [ -n "$unit" ] || continue
        if [ "$enabled" -eq 1 ] && ! unit_is_enabled "$unit"; then
            systemctl enable "$unit" >/dev/null 2>&1 || failures=$((failures + 1))
        fi
        # restart, not start: a unit that stayed active through the failure is
        # still running the code that was just rolled back underneath it.
        if [ "$active" -eq 1 ]; then
            systemctl restart "$unit" >/dev/null 2>&1 || failures=$((failures + 1))
        fi
    done < "$TXN_DIR/units.tsv"

    if [ "$failures" -eq 0 ]; then
        warn "rollback complete; the previous state was restored"
    else
        warn "rollback finished with $failures problem(s) - check the system by hand"
    fi
    warn "backup and manifest kept at: $BACKUP_DIR"
    warn "  files:    $TXN_DIR/files"
    warn "  manifest: $TXN_DIR/manifest.tsv"
    warn "  units:    $TXN_DIR/units.tsv"
    set -e
    [ "$failures" -eq 0 ]
}

on_error() {
    # Accept an explicit status: when this is reached through `if ! validate`,
    # $? is already 0 because the negation succeeded, and exiting 0 would
    # report a rolled-back install as a success.
    local code=${1:-$?}
    [ "$code" -ne 0 ] || code=1
    printf '\n%serror%s installation failed (exit %d)\n' "$RED" "$RESET" "$code" >&2
    rollback || true
    exit "$code"
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
install_files() {
    step "Installing files"
    txn_mkdir "$STATE_DIR" 0755
    run install -m 0755 "$REPO_ROOT/bin/qnap-tsx70-lcd" "$BIN_DIR/qnap-tsx70-lcd"
    ok "installed $BIN_DIR/qnap-tsx70-lcd"

    if [ -f "$CONF_FILE" ]; then
        ok "kept existing $CONF_FILE (see config/qnap-tsx70-lcd.conf.example for new keys)"
    else
        run install -m 0644 "$REPO_ROOT/config/qnap-tsx70-lcd.conf.example" "$CONF_FILE"
        ok "installed $CONF_FILE"
    fi

    run install -m 0644 "$REPO_ROOT/systemd/qnap-tsx70-lcd.service" \
        "$UNIT_DIR/$NEW_LCD_UNIT"
    ok "installed $NEW_LCD_UNIT"

    if [ "$WITH_FAN" -eq 1 ]; then
        run install -m 0755 "$REPO_ROOT/bin/qnap-tsx70-fancontrol" \
            "$BIN_DIR/qnap-tsx70-fancontrol"
        run install -m 0644 "$REPO_ROOT/systemd/qnap-tsx70-fancontrol.service" \
            "$UNIT_DIR/$NEW_FAN_UNIT"
        txn_mkdir "$(dirname "$MODULES_FILE")" 0755
        run sh -c "printf 'coretemp\nf71882fg\n' > '$MODULES_FILE'"
        run modprobe f71882fg 2>/dev/null || warn "modprobe f71882fg failed"
        run modprobe coretemp 2>/dev/null || true
        ok "installed fan control (experimental)"
    fi
}

enable_services() {
    step "Enabling services"
    run systemctl daemon-reload
    run systemctl enable "$NEW_LCD_UNIT"
    run systemctl restart "$NEW_LCD_UNIT"
    ok "qnap-tsx70-lcd enabled and started"

    if [ "$WITH_FAN" -eq 1 ]; then
        # Enabling a fan service that has no calibration means the next boot
        # starts it, it exits, and systemd burns its start limit. So the unit
        # is enabled only when a cache is already there and validates.
        # cache_is_valid only reads, so the decision is reported in a dry
        # run too rather than being silently skipped.
        if cache_is_valid "$CACHE_FILE"; then
            run systemctl enable "$NEW_FAN_UNIT"
            ok "qnap-tsx70-fancontrol enabled: $CACHE_FILE validates"
            info "       it is NOT started here; start it when you are watching:"
            info "         systemctl start qnap-tsx70-fancontrol"
        else
            if unit_is_enabled "$NEW_FAN_UNIT"; then
                warn "$NEW_FAN_UNIT is already enabled but $CACHE_FILE does not"
                warn "validate. systemd will skip it at boot (ConditionPathExists=)."
                warn "Either calibrate it, or disable it:"
                warn "  systemctl disable qnap-tsx70-fancontrol"
            fi
            ok "qnap-tsx70-fancontrol installed but NOT enabled and NOT started"
            info "       there is no valid calibration at $CACHE_FILE, and a fan"
            info "       service enabled without one fails at every boot until it"
            info "       hits systemd's start limit. Calibrate first:"
            info "         qnap-tsx70-fancontrol calibrate --yes"
            info "         systemctl enable --now qnap-tsx70-fancontrol"
        fi
    fi
}

validate() {
    step "Validation"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "       [dry-run] skipping runtime validation"
        return 0
    fi
    sleep "$SETTLE_SECS"
    if unit_is_active "$NEW_LCD_UNIT"; then
        ok "qnap-tsx70-lcd is active"
    else
        warn "qnap-tsx70-lcd is not active:"
        systemctl status --no-pager --lines=15 "$NEW_LCD_UNIT" >&2 || true
        return 1
    fi
    "$BIN_DIR/qnap-tsx70-lcd" --config "$CONF_FILE" --check || \
        warn "preflight reported warnings; the service is running anyway"
    return 0
}

# post_cleanup_validate - the transaction is only finished once the legacy
# cleanup has also left a working system behind.
# post_cleanup_validate <migrated> - the transaction is only finished once
# cleanup has also left a working system behind. With --no-migrate there was
# no cleanup and legacy units are supposed to still be there.
post_cleanup_validate() {
    local migrated="$1"
    step "Post-cleanup validation"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "       [dry-run] skipping post-cleanup validation"
        return 0
    fi
    if ! unit_is_active "$NEW_LCD_UNIT"; then
        warn "qnap-tsx70-lcd stopped being active during legacy cleanup"
        return 1
    fi
    if [ "$migrated" -ne 1 ]; then
        ok "the new installation is active"
        return 0
    fi
    local unit
    for unit in "${LEGACY_UNITS[@]}"; do
        if [ -f "$UNIT_DIR/$unit" ]; then
            warn "$UNIT_DIR/$unit survived cleanup"
            return 1
        fi
    done
    ok "the new installation is active and the legacy units are gone"
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
    fi

    # The transaction covers a plain reinstall too: it replaces the same
    # binaries and units a migration does, and an interrupted reinstall has
    # exactly the same need to be undone.
    step "Recording the current state"
    trap 'die "could not prepare the transaction manifest under $BACKUP_ROOT; nothing has been changed"' ERR
    txn_begin
    txn_snapshot_all
    make_backup
    trap - ERR

    # Armed as soon as anything can change, and kept armed through legacy
    # cleanup, daemon-reload and post-cleanup validation.
    trap 'on_error $?' ERR
    ROLLBACK_NEEDED=1

    stop_current_services
    verify_port_released
    if [ "$legacy" -eq 1 ]; then
        migrate_cache
    fi

    install_files
    enable_services
    if ! validate; then
        on_error 1
    fi

    if [ "$legacy" -eq 1 ]; then
        remove_legacy
    fi
    if ! post_cleanup_validate "$legacy"; then
        on_error 1
    fi

    ROLLBACK_NEEDED=0
    trap - ERR

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
        info "  systemctl enable --now qnap-tsx70-fancontrol"
    fi
    if [ "$legacy" -eq 1 ]; then
        info ""
        info "Legacy saturn-* artifacts were migrated and removed."
        if [ "$CACHE_PRESERVED" -eq 1 ]; then
            info "The legacy calibration cache was NOT valid, so it was kept at"
            info "  $LEGACY_CACHE"
        fi
        info "Backup: $BACKUP_DIR (see docs/MIGRATION_FROM_SATURN.md)"
    fi
}

main "$@"
