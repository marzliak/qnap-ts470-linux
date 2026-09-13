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
#   QNAP_TSX70_DEVICE_GLOB  platform-device pattern for the fan controller
#   QNAP_TSX70_FAN_BINARY   fan-control program used to verify the handback
#
# systemctl, fuser, modprobe and python3 are resolved from PATH, so a test
# supplies fakes by putting them earlier in PATH.
# ---------------------------------------------------------------------------
TEST_ROOT="${QNAP_TSX70_TEST_ROOT:-}"
PROC_DIR="${QNAP_TSX70_PROC_DIR:-/proc}"
KILL_CMD="${QNAP_TSX70_KILL:-kill}"
SETTLE_SECS="${QNAP_TSX70_SETTLE_SECS:-2}"
STOP_TIMEOUT="${QNAP_TSX70_STOP_TIMEOUT:-20}"
FAN_DEVICE_GLOB="${QNAP_TSX70_DEVICE_GLOB:-/sys/devices/platform/f71882fg.*}"
# The repository copy, not an installed one: the handback verdict has to come
# from the version being shipped, because an older installed binary is exactly
# the thing whose behaviour is not trusted here. It is the same rule
# cache_is_valid already follows for the calibration schema.
FAN_BINARY="${QNAP_TSX70_FAN_BINARY:-$REPO_ROOT/bin/qnap-tsx70-fancontrol}"
# Deliberately not a seam. The lock root already has one - QNAP_TSX70_LOCK_DIR,
# honoured by the fan program itself, which is how two processes are made to
# contend on purpose - and a second override naming the helper would let a
# caller point this run at a lock no writer takes.
FAN_LOCK_HELPER="$REPO_ROOT/scripts/qnap_lock.py"

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

# Every fan unit and executable a rollback can put back or take away, whatever
# this run's install-time scope was. The scope says what the install meant to
# change; a rollback undoes the whole transaction, and txn_snapshot_all
# records all of these, so the quiescence gate in front of it covers the same
# set rather than the narrower one.
ROLLBACK_FAN_UNITS=("$NEW_FAN_UNIT" "${LEGACY_FAN_UNITS[@]}")
ROLLBACK_FAN_BINS=(qnap-tsx70-fancontrol "${LEGACY_FAN_BINS[@]}")

# Executables that may legitimately hold the panel's serial port.
LCD_OWNER_BINS=(qnap-tsx70-lcd "${LEGACY_LCD_BINS[@]}")
# There is deliberately no equivalent all-fan-executables list. Which fan
# binaries matter depends on what a given run replaces or removes, and a gate
# over every name this script knows is what stopped an LCD-only install
# because of a fan service it was never going to touch. plan_scope decides.
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
# What this run actually changed, as opposed to what it merely recorded.
# TXN_PATHS is the answer to "what was here before?" and covers every path
# this script is capable of touching; these two answer "did this run touch
# it?", and that is the question a rollback near fan control must ask.
TXN_MUTATED_PATHS=()
TXN_MUTATED_UNITS=()
ROLLBACK_NEEDED=0
ROLLBACK_RUNNING=0
CACHE_MIGRATED=0
CACHE_PRESERVED_INVALID=0
CACHE_SUPERSEDED=0

# What this run is actually going to replace or remove. Nothing else may be
# stopped. An LCD-only install does not touch the fan binary or its unit, so
# stopping an fan service that was running is a change to the machine the
# operator did not ask for; --no-migrate promises the legacy tree is left
# alone, which includes leaving its services running. Filled in by plan_scope
# once the legacy decision is known, and read by every stop and every
# quiescence gate.
LEGACY_IN_SCOPE=0
# Set once `qnap-tsx70-fancontrol safe-state` has exited 0 in this run. Nothing
# else may set it, and no fan recovery artifact is deleted or replaced without
# it.
FAN_HANDBACK_VERIFIED=0
# The descriptor this shell holds the canonical fan writer lock on, and the
# path it names. Empty means "not held": see fan_lock_acquire.
FAN_LOCK_FD=""
FAN_LOCK_PATH=""
FAN_SCOPE_UNITS=()
FAN_SCOPE_BINS=()
LCD_SCOPE_UNITS=()
FAN_UNIT_WAS_ACTIVE=0
# The rollback's own fan verdict, which is a different question from
# FAN_HANDBACK_VERIFIED above and is never derived from it: 0 no fan artifact
# is at risk in this rollback, 1 a fresh quiescence and handback proved the
# fans are back, 2 it could not be proved and no fan artifact may be touched.
ROLLBACK_FAN_GATE=0
ROLLBACK_SKIPPED_FAN=()

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
                        unrecognised process holds the serial port, nor when
                        who holds it cannot be determined at all.
  --allow-serial-owner PID
                        Accept PID as the current holder of the serial port
                        even though it is not one of this project's services.
                        The installer still never signals it, and still refuses
                        to continue unless the port is actually free by the
                        time the service has to open it. It needs fuser or
                        lsof like everything else here: without one, ownership
                        cannot be checked and the install stops instead.
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

# ---------------------------------------------------------------------------
# Fan-control command grammar
#
# `qnap-tsx70-fancontrol status` in another terminal, and this installer's own
# validate-cache child, are not fan writers; treating them as ones would stall
# every run for STOP_TIMEOUT and then abort.
#
# What must never happen is the reverse. The first version of this test asked
# whether *any* argv token equalled one of the read-only words, so
# `qnap-tsx70-fancontrol run --sensor status` - a live control loop - was
# classified read-only and the installer deleted the binary out from under it.
# So the command is parsed structurally instead: find the executable in argv,
# then walk forward past options and their values to the command word. The
# grammar below is bin/qnap-tsx70-fancontrol's own (see build_parser there):
# the command is a positional that argparse requires before any of the
# per-command options, so nothing after it can change what it is.
# ---------------------------------------------------------------------------
FAN_READ_ONLY_COMMANDS=(status validate-cache --version --help -h)
FAN_WRITER_COMMANDS=(run calibrate safe-state)
# Options that consume the token after them, so a value can never be mistaken
# for the command word.
FAN_VALUE_OPTIONS=(--device-glob --cache --sensor --interval --temp-min
                   --temp-max --channels)

# fan_command_of <pid> <name>... - print the fan-control command word a
# process is running. Nonzero when argv contains none of <name>..., or when no
# command word can be identified; every caller treats that as a writer.
fan_command_of() {
    local pid="$1"; shift
    local names=("$@")
    local token state=before skip=0
    while IFS= read -r token; do
        if [ "$skip" -eq 1 ]; then skip=0; continue; fi
        case "$state" in
            # Everything up to and including the executable belongs to an
            # interpreter: `/usr/bin/python3 -u /usr/local/bin/... run`.
            before)
                if in_list "${token##*/}" "${names[@]}"; then state=after; fi
                continue ;;
            # After a `--` separator the next token is the command, whatever
            # it looks like.
            positional) printf '%s' "$token"; return 0 ;;
        esac
        case "$token" in
            "") continue ;;
            -h|--help|--version) printf '%s' "$token"; return 0 ;;
            --) state=positional; continue ;;
            --*=*) continue ;;
            -*)
                if in_list "$token" "${FAN_VALUE_OPTIONS[@]}"; then skip=1; fi
                continue ;;
            *) printf '%s' "$token"; return 0 ;;
        esac
    done < <(pid_argv "$pid" || true)
    return 1
}

# fan_pid_class <pid> <name>... - "read-only", "writer" or "unknown".
#
# "unknown" is the fail-closed answer and covers a command this version does
# not know, an invocation with no command word at all, and a process whose
# argv does not contain one of <name>... (a legacy saturn-* daemon with its
# own grammar, or a name that only matched comm). Every caller treats it like
# a writer, because the cost is not symmetric: a false writer is a refusal the
# operator can act on, a false reader deletes the binary out from under a
# process driving PWM registers.
fan_pid_class() {
    local pid="$1"; shift
    local verb
    if ! verb="$(fan_command_of "$pid" "$@")"; then
        printf 'unknown'
        return 0
    fi
    if in_list "$verb" "${FAN_READ_ONLY_COMMANDS[@]}"; then
        printf 'read-only'
    elif in_list "$verb" "${FAN_WRITER_COMMANDS[@]}"; then
        printf 'writer'
    else
        printf 'unknown'
    fi
}

# pid_is_read_only <pid> <name>... - true only for an invocation that is
# structurally one of the read-only commands.
pid_is_read_only() {
    [ "$(fan_pid_class "$@")" = "read-only" ]
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
        if pid_may_be "$pid" "$@" && ! pid_is_read_only "$pid" "$@"; then
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
SERIAL_OWNER_OUT_OF_SCOPE=""

classify_serial_owner() {
    local port="$1" pids pid unit
    if ! can_inspect_port; then
        SERIAL_OWNER_KIND="unknowable"
        SERIAL_OWNER_PIDS=""
        return 0
    fi
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
            note_out_of_scope_owner "$pid"
            continue
        fi
        # systemd knows which process belongs to which unit even when the
        # executable has already been replaced on disk.
        local matched=0
        for unit in "$NEW_LCD_UNIT" "${LEGACY_LCD_UNITS[@]}"; do
            [ "$(unit_main_pid "$unit")" = "$pid" ] && { matched=1; break; }
        done
        if [ "$matched" -eq 1 ]; then
            note_out_of_scope_owner "$pid"
            continue
        fi
        all_known=0
        warn "$port is held by an unrecognised process: $(pid_describe "$pid")"
    done
    if [ "$all_known" -eq 1 ]; then
        SERIAL_OWNER_KIND="known"
    else
        SERIAL_OWNER_KIND="unknown"
    fi
}

# note_out_of_scope_owner <pid> - one of this project's own LCD processes holds
# the port, but this run is not going to stop it.
#
# "Recognised" used to be the end of the question, and preflight then promised
# the holder "will be stopped as part of the planned transition". With
# --no-migrate that promise is false: the legacy LCD is deliberately left
# alone, so the port never comes free and the new service fails to open it
# after the install has already replaced everything. Better to say so before
# the first mutation.
note_out_of_scope_owner() {
    local pid="$1" unit
    if [ "$LEGACY_IN_SCOPE" -eq 1 ]; then
        return 0
    fi
    if pid_is_definitely "$pid" "${LEGACY_LCD_BINS[@]}"; then
        SERIAL_OWNER_OUT_OF_SCOPE="${SERIAL_OWNER_OUT_OF_SCOPE}$pid "
        return 0
    fi
    for unit in "${LEGACY_LCD_UNITS[@]}"; do
        if [ "$(unit_main_pid "$unit")" = "$pid" ]; then
            SERIAL_OWNER_OUT_OF_SCOPE="${SERIAL_OWNER_OUT_OF_SCOPE}$pid "
            return 0
        fi
    done
    return 0
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
        classify_serial_owner "$SERIAL_PORT"
        case "$SERIAL_OWNER_KIND" in
            free) ok "serial port is free" ;;
            unknowable)
                # Fail closed, with no way past it. --allow-serial-owner used
                # to double as the escape hatch here, which was dishonest: with
                # no fuser and no lsof there is nothing to check the named PID
                # against, no way to tell whether it even holds the port, and
                # no way to confirm the port is free before the new service
                # opens it. The only truthful answer is to ask for a tool.
                warn "neither fuser nor lsof is installed, so who holds"
                warn "$SERIAL_PORT cannot be determined - and an unverifiable"
                warn "port is not a free one. Install one of them first:"
                warn "  apt-get install psmisc     # provides fuser"
                warn "  apt-get install lsof"
                warn "Neither --force nor --allow-serial-owner covers this:"
                warn "there is nothing here for either of them to be right about."
                failed=1
                ;;
            known)
                if [ "$SERIAL_OWNER_OVERRIDDEN" -eq 1 ]; then
                    ok "serial port owner accepted (pids: $SERIAL_OWNER_PIDS)"
                elif [ -n "$SERIAL_OWNER_OUT_OF_SCOPE" ]; then
                    warn "$SERIAL_PORT is held by a legacy LCD process (pids:"
                    warn "${SERIAL_OWNER_OUT_OF_SCOPE% })"
                    warn "and --no-migrate means this run leaves the legacy"
                    warn "installation, that process included, exactly as it is."
                    warn "The new LCD service could not then open the port."
                    warn "Stop it yourself, or drop --no-migrate and let the"
                    warn "migration stop it after the backup exists."
                    failed=1
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
        if [ -n "$ALLOW_SERIAL_OWNER" ] && [ "$SERIAL_OWNER_OVERRIDDEN" -eq 1 ]; then
            warn "accepting pid $ALLOW_SERIAL_OWNER as the serial port owner on your word;"
            warn "it will not be signalled, and the port must be free before the service starts"
        fi
    else
        warn "serial port $SERIAL_PORT not found (is this a TS-x70 with the A125 panel?)"
        [ "$FORCE" -eq 1 ] || failed=1
    fi

    if [ "$WITH_FAN" -eq 1 ]; then
        local matches
        matches="$(compgen -G "$FAN_DEVICE_GLOB" | wc -l || true)"
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

# txn_touch_path <path> - this run changed this file: wrote it, replaced it
# or deleted it. Recorded separately from the manifest because the manifest
# is a snapshot of everything this script could touch, and "could" is not
# "did". A rollback that treats the two as the same thing undoes changes
# nobody made - which for a fan binary means replacing the executable an
# operator is recovering with, during a failure that had nothing to do with
# fan control.
txn_touch_path() {
    in_list "$1" ${TXN_MUTATED_PATHS[@]+"${TXN_MUTATED_PATHS[@]}"} && return 0
    TXN_MUTATED_PATHS+=("$1")
    return 0
}

# txn_touch_unit <unit> - this run changed what this unit is doing: stopped
# it, started it, enabled it or disabled it.
txn_touch_unit() {
    in_list "$1" ${TXN_MUTATED_UNITS[@]+"${TXN_MUTATED_UNITS[@]}"} && return 0
    TXN_MUTATED_UNITS+=("$1")
    return 0
}

txn_path_was_mutated() {
    in_list "$1" ${TXN_MUTATED_PATHS[@]+"${TXN_MUTATED_PATHS[@]}"}
}

txn_unit_was_mutated() {
    in_list "$1" ${TXN_MUTATED_UNITS[@]+"${TXN_MUTATED_UNITS[@]}"}
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
                if [ "$DRY_RUN" -eq 1 ]; then
                    printf '       [dry-run] record %s as enabled\n' "$unit"
                else
                    printf '%s\n' "$unit" >> "$BACKUP_DIR/enabled-units"
                fi
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
    # Past this line the unit's activity is this run's doing. The early return
    # above is not: a unit that was already inactive was not stopped by us,
    # and a rollback has nothing of its to put back.
    txn_touch_unit "$unit"
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
    warn "  systemctl status ${FAN_SCOPE_UNITS[*]}"
    warn "  systemctl stop   ${FAN_SCOPE_UNITS[*]}"
    warn "  qnap-tsx70-fancontrol safe-state    # hands the fans back to the chip"
    warn "then re-run this installer."
}

# ---------------------------------------------------------------------------
# The canonical fan writer lock
#
# Stopping a unit and then reading the process table proves something about
# one instant. Between that instant and the `install` or the `rm` that follows
# it, a `systemctl start qnap-tsx70-fancontrol`, a boot-time activation or an
# operator's `calibrate` can begin - and that is a fan writer whose executable
# is about to be replaced or deleted underneath it, which is precisely what
# the check was there to prevent. The window closes only by holding the lock
# the writers themselves take, across the whole of it.
#
# It is the same lock, not a second one that resembles it: scripts/qnap_lock.py
# reads the path out of bin/qnap-tsx70-fancontrol rather than defining one, so
# the two sides cannot drift apart. Only the canonical global lock is taken -
# the one every writer takes first, whatever controller it resolved and
# whatever --cache it was given - so holding it excludes all of them, no
# device discovery is needed here, and no flag any operator can pass moves it.
#
# The lock lives on a descriptor this shell owns, so releasing it is closing a
# file. That makes the cleanup unconditional rather than careful: every exit
# releases it, including the ones no trap can run for, so an installer killed
# mid-run leaves no lock claiming an owner that no longer exists. The explicit
# release below exists for the opposite reason - a fan service started while
# this still held the lock would be refused at its own and fail to come up, so
# the hold has to end at the last protected mutation and not at the end of the
# script. install_files() and rollback() are where each of those ends.
#
# safe-state deliberately takes no lock, so the verified handback below still
# works while this is held. That is the property that keeps this from
# deadlocking against the emergency recovery it depends on.
# ---------------------------------------------------------------------------
fan_lock_acquire() {
    [ -n "$FAN_LOCK_FD" ] && return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] hold the fan writer lock across the recheck, the handback and every replacement\n'
        return 0
    fi
    if [ ! -f "$FAN_LOCK_HELPER" ]; then
        warn "no lock helper at $FAN_LOCK_HELPER, so the fan writer lock"
        warn "cannot be held and a writer could start between the check below"
        warn "and the files this run replaces"
        return 1
    fi
    local path
    if ! path="$(python3 "$FAN_LOCK_HELPER" path 2>&1)" || [ -z "$path" ]; then
        warn "could not resolve the canonical fan writer lock path: $path"
        return 1
    fi
    if ! mkdir -p "$(dirname "$path")"; then
        warn "could not create the lock directory $(dirname "$path")"
        return 1
    fi
    # Appended to, never truncated: another process's recorded PID is not this
    # script's to erase, and the lock is on the descriptor either way.
    if ! exec {FAN_LOCK_FD}>>"$path"; then
        FAN_LOCK_FD=""
        warn "could not open the fan writer lock $path"
        return 1
    fi
    if python3 "$FAN_LOCK_HELPER" acquire --fd "$FAN_LOCK_FD"; then
        FAN_LOCK_PATH="$path"
        ok "holding the fan writer lock $path"
        return 0
    fi
    exec {FAN_LOCK_FD}>&-
    FAN_LOCK_FD=""
    warn "another fan writer holds $path. Stop it and re-run:"
    warn "  systemctl stop $NEW_FAN_UNIT"
    return 1
}

# fan_lock_release - give the lock up deliberately, before a fan service is
# allowed to start. Never fails: closing a descriptor this shell owns cannot,
# and every caller is on a path that must not abort here.
fan_lock_release() {
    [ -n "$FAN_LOCK_FD" ] || return 0
    exec {FAN_LOCK_FD}>&-
    FAN_LOCK_FD=""
    ok "released the fan writer lock $FAN_LOCK_PATH"
    return 0
}

# stop_fan_services - every fan writer this run is about to replace or delete,
# off and proven off. Units first, then the bare legacy daemon named by its PID
# file, and only then the quiescence gate: the PID-file process is a fan writer
# nothing else supervises, so checking for it before asking it to stop would
# always abort.
#
# Nothing happens here when the scope is empty, which is the LCD-only,
# no-migration case: this run writes no fan binary and deletes none, so a fan
# service that is running is not its business, is left running, and the writer
# lock is not taken either - an operation with no fan artifact in scope has no
# reason to exclude the writers.
stop_fan_services() {
    local unit
    if [ "${#FAN_SCOPE_UNITS[@]}" -eq 0 ]; then
        ok "no fan artifact is being replaced or removed; fan services left as they are"
        return 0
    fi
    for unit in "${FAN_SCOPE_UNITS[@]}"; do
        unit_exists "$unit" || unit_is_active "$unit" || continue
        if [ "$unit" = "$NEW_FAN_UNIT" ] && unit_is_active "$unit"; then
            # Remembered so a successful install puts it back the way it was
            # found instead of silently leaving fan control down.
            FAN_UNIT_WAS_ACTIVE=1
        fi
        if ! stop_unit_verified "$unit"; then
            fan_recovery_hint
            return 1
        fi
    done
    if [ "$LEGACY_IN_SCOPE" -eq 1 ] && ! stop_legacy_pid_process; then
        fan_recovery_hint
        return 1
    fi
    # Here, and not after the recheck: the lock is what makes the recheck
    # mean anything past the instant it runs. Taken once the units this run
    # owns are down, so it is not contending with a writer that was about to
    # be stopped anyway, and held from here through the handback, the file
    # replacements and the legacy deletions.
    if ! fan_lock_acquire; then
        fan_recovery_hint
        return 1
    fi
    if ! wait_for_no_process "fan control" "${FAN_SCOPE_BINS[@]}"; then
        fan_recovery_hint
        return 1
    fi
    return 0
}

# legacy_fan_artifacts_present - is there a legacy fan binary or unit on this
# machine that a migration would delete? The cache is not one: it is data, it
# drives nothing, and it has its own provenance rules.
legacy_fan_artifacts_present() {
    local unit bin
    for unit in "${LEGACY_FAN_UNITS[@]}"; do
        [ -f "$UNIT_DIR/$unit" ] && return 0
    done
    for bin in "${LEGACY_FAN_BINS[@]}"; do
        [ -f "$BIN_DIR/$bin" ] && return 0
    done
    return 1
}

# fan_artifacts_at_risk - does this run delete or replace a file that is
# somebody's way back from a fan stuck in manual mode?
#
# Only the binary and the unit count. A fresh --with-fan-control install on a
# machine that has neither is adding a recovery mechanism, not removing one,
# so it is not at risk and does not have to prove anything about a controller
# whose driver this run has not even loaded yet.
fan_artifacts_at_risk() {
    if [ "$LEGACY_IN_SCOPE" -eq 1 ] && legacy_fan_artifacts_present; then
        return 0
    fi
    if [ "$WITH_FAN" -eq 1 ]; then
        [ -f "$BIN_DIR/qnap-tsx70-fancontrol" ] && return 0
        [ -f "$UNIT_DIR/$NEW_FAN_UNIT" ] && return 0
    fi
    return 1
}

# verify_fan_handback - prove the chip has the fans before this run deletes or
# replaces the executable that is how anyone would prove it afterwards.
#
# An inactive unit and an empty process table are both equally true of a
# machine whose fan is parked at a calibration stall duty with pwmN_enable
# still reading 1. Neither says anything about the PWM registers. The only
# evidence is a write followed by a readback, which is what `safe-state` does
# and what its exit status - and nothing else about it, not its output and not
# the fact that it ran - reports.
#
# Scope is half the point: a run that is not removing or replacing a fan
# recovery artifact has no business writing to a fan controller at all, so an
# LCD-only install never reaches the command.
#
# This cannot be delegated to the unit's ExecStopPost=. That line carries a
# `-` prefix, which tells systemd to ignore its exit status, so it is a
# best-effort cleanup and never a verdict.
verify_fan_handback() {
    if ! fan_artifacts_at_risk; then
        ok "no fan binary or unit is being replaced or removed; the fan controller is left alone"
        return 0
    fi
    step "Confirming the fans are back under the controller"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '       [dry-run] python3 %s safe-state\n' "$FAN_BINARY"
        printf '       [dry-run] replacing or deleting a fan binary or unit is gated on its exit status\n'
        return 0
    fi
    if [ ! -f "$FAN_BINARY" ]; then
        warn "no fan-control program at $FAN_BINARY, so the handback cannot be"
        warn "verified. The fan binary and unit already on this machine are the"
        warn "only way back from a fan left in manual mode, so they stay."
        fan_handback_hint
        return 1
    fi
    if fan_handback_now; then
        FAN_HANDBACK_VERIFIED=1
        return 0
    fi
    warn "the fans could not be confirmed back in the controller's automatic"
    warn "mode, so nothing that could put them there is being removed or"
    warn "replaced. Every fan binary and unit on this machine stays where it is."
    fan_handback_hint
    return 1
}

# fan_handback_now - ask the chip and read the answer back. No scope decision
# and no state of its own: the caller has already decided that it has to prove
# this, and the exit status of `safe-state` is the whole verdict. Its output
# is reported because an operator needs it, and decides nothing.
fan_handback_now() {
    if [ ! -f "$FAN_BINARY" ]; then
        warn "there is no fan-control program at $FAN_BINARY to ask"
        return 1
    fi
    local out
    if out="$(python3 "$FAN_BINARY" safe-state --device-glob "$FAN_DEVICE_GLOB" 2>&1)"; then
        ok "automatic fan mode verified on the controller"
        return 0
    fi
    [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/       /' >&2
    return 1
}

fan_handback_hint() {
    warn "Recover, then re-run this installer:"
    warn "  modprobe f71882fg                                  # if the driver is not loaded"
    warn "  python3 $FAN_BINARY status"
    warn "  python3 $FAN_BINARY safe-state     # must exit 0"
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

# plan_scope <legacy> - decide what this run owns.
#
# A service is only stopped when this run replaces or deletes the binary or
# unit behind it, because that is the only thing a stop is protecting against.
# The new fan unit is in scope only with --with-fan-control, since without it
# install_files never writes bin/qnap-tsx70-fancontrol. The legacy tree is in
# scope only when a migration is actually going to remove it.
plan_scope() {
    local legacy="$1"
    LEGACY_IN_SCOPE="$legacy"
    FAN_SCOPE_UNITS=()
    FAN_SCOPE_BINS=()
    LCD_SCOPE_UNITS=()
    if [ "$legacy" -eq 1 ]; then
        FAN_SCOPE_UNITS+=("${LEGACY_FAN_UNITS[@]}")
        FAN_SCOPE_BINS+=("${LEGACY_FAN_BINS[@]}")
        LCD_SCOPE_UNITS+=("${LEGACY_LCD_UNITS[@]}")
    fi
    if [ "$WITH_FAN" -eq 1 ]; then
        FAN_SCOPE_UNITS+=("$NEW_FAN_UNIT")
        FAN_SCOPE_BINS+=(qnap-tsx70-fancontrol)
    fi
    # The new LCD unit is always replaced, so it is always in scope.
    LCD_SCOPE_UNITS+=("$NEW_LCD_UNIT")
}

# stop_current_services - the planned transition. Fan writers first, then the
# LCD, so the serial port is free before anything reopens it. Both lists are
# the scope, not everything this installer knows how to name.
stop_current_services() {
    step "Stopping services for the transition"
    stop_fan_services || return 1
    local unit
    for unit in "${LCD_SCOPE_UNITS[@]}"; do
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
        # Preflight refuses this case outright, so reaching it means fuser or
        # lsof disappeared mid-run. There is still no honest way to claim the
        # port is free, and --allow-serial-owner cannot make one.
        warn "cannot confirm $SERIAL_PORT is free without fuser or lsof"
        warn "the LCD service is not started on an unverified port"
        return 1
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
        CACHE_SUPERSEDED=1
        ok "kept the existing calibration at $CACHE_FILE"
        info "       the legacy cache was not migrated over it, so it is left"
        info "       at $LEGACY_CACHE as well"
    elif cache_is_valid "$LEGACY_CACHE"; then
        install -m 0644 "$LEGACY_CACHE" "$CACHE_FILE"
        txn_touch_path "$CACHE_FILE"
        CACHE_MIGRATED=1
        ok "migrated calibration cache to $CACHE_FILE"
    else
        CACHE_PRESERVED_INVALID=1
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
    if ! wait_for_no_process "fan control" "${FAN_SCOPE_BINS[@]}"; then
        fan_recovery_hint
        return 1
    fi
    # Restating the gate over the deletions themselves. No current ordering
    # can reach this - a legacy fan artifact present now was present when
    # verify_fan_handback ran, and that run either verified or aborted - so
    # this is not where the protection lives. It is here so that moving the
    # gate, or adding a path that skips it, fails closed instead of quietly
    # deleting the one executable that can put the fans back. The dry-run
    # exemption is not a loophole: a dry run deletes nothing.
    if [ "$DRY_RUN" -eq 0 ] && legacy_fan_artifacts_present \
       && [ "$FAN_HANDBACK_VERIFIED" -ne 1 ]; then
        warn "refusing to delete the legacy fan artifacts: this run never"
        warn "confirmed the fans were back in the controller's automatic mode"
        fan_handback_hint
        return 1
    fi
    local unit bin
    for unit in "${LEGACY_UNITS[@]}"; do
        unit_exists "$unit" || continue
        run_quiet systemctl disable "$unit"
        txn_touch_unit "$unit"
        if [ -f "$UNIT_DIR/$unit" ]; then
            run rm -f "$UNIT_DIR/$unit"
            txn_touch_path "$UNIT_DIR/$unit"
            [ "$DRY_RUN" -eq 0 ] && ok "removed $unit"
        else
            # Known to systemd but not ours to delete: it is shipped
            # somewhere like /lib/systemd/system. Disabling is all we can
            # honestly claim.
            ok "disabled $unit (its unit file is not in $UNIT_DIR)"
        fi
    done
    for bin in "${LEGACY_BINS[@]}"; do
        if [ -f "$BIN_DIR/$bin" ]; then
            run rm -f "$BIN_DIR/$bin"
            txn_touch_path "$BIN_DIR/$bin"
            [ "$DRY_RUN" -eq 0 ] && ok "removed $BIN_DIR/$bin"
        fi
    done
    if [ -f "$LEGACY_CACHE" ]; then
        if [ "$CACHE_MIGRATED" -eq 1 ] && cache_is_valid "$CACHE_FILE"; then
            run rm -f "$LEGACY_CACHE"
            txn_touch_path "$LEGACY_CACHE"
            ok "removed $LEGACY_CACHE (migrated and validated)"
        elif [ "$CACHE_SUPERSEDED" -eq 1 ]; then
            info "       kept $LEGACY_CACHE: a newer calibration is already in"
            info "       place, so it was not migrated over it"
        else
            CACHE_PRESERVED_INVALID=1
            warn "kept $LEGACY_CACHE: it did not validate, so it was not"
            warn "migrated and it is the only copy outside the backup"
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
            txn_touch_path "$LEGACY_PID"
            ok "removed $LEGACY_PID"
        fi
    fi
    run systemctl daemon-reload
    if [ "$CACHE_PRESERVED_INVALID" -eq 1 ] || [ "$CACHE_SUPERSEDED" -eq 1 ]; then
        info "       legacy artifacts removed except the calibration cache noted above"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Rollback
#
# A rollback can be reached from anywhere after the first change, including
# from the last line of main(), where restore_fan_activity() has just started
# a fan service that was running before the install back up. By the time the
# restore below would replace the fan binary and the unit file, that service
# is a live writer again - and FAN_HANDBACK_VERIFIED, earned before any of it,
# describes a machine that no longer exists. Putting a file back is the same
# act as deleting it as far as a fan parked in manual mode is concerned: it
# takes away the executable an operator would recover with, for as long as it
# takes, and hands the unit a different binary. So the rollback proves it
# again, from scratch, or it leaves every fan recovery artifact exactly where
# this install left it and says so.
#
# All of which applies only to the fan artifacts this transaction actually
# changed. A rollback that would restore no fan file and restart no fan unit
# has no fan hazard to gate, and reaching for one - stopping the service,
# scanning for its processes, writing to the controller - is itself the
# hazard. rollback_touches_fan_artifacts() is where that line is drawn.
# ---------------------------------------------------------------------------

# is_fan_artifact_path <path> - is this file somebody's way back from a fan
# stuck in manual mode? The cache is not one: it is data and drives nothing.
is_fan_artifact_path() {
    local path="$1" name
    [ "$path" = "$BIN_DIR/qnap-tsx70-fancontrol" ] && return 0
    [ "$path" = "$UNIT_DIR/$NEW_FAN_UNIT" ] && return 0
    for name in "${LEGACY_FAN_UNITS[@]}"; do
        [ "$path" = "$UNIT_DIR/$name" ] && return 0
    done
    for name in "${LEGACY_FAN_BINS[@]}"; do
        [ "$path" = "$BIN_DIR/$name" ] && return 0
    done
    return 1
}

is_fan_unit() {
    local unit="$1" name
    for name in "${ROLLBACK_FAN_UNITS[@]}"; do
        [ "$unit" = "$name" ] && return 0
    done
    return 1
}

# rollback_touches_fan_artifacts - would undoing this transaction change a fan
# binary, a fan unit file, or what a fan unit is doing?
#
# Answered from what this run actually changed, not from what it recorded.
# That distinction is the whole finding: txn_snapshot_all() records every path
# this script is capable of touching, including the fan binary and both fan
# units, on every run. Reading the manifest made an LCD-only install on a
# machine that happens to have fan control look exactly like a fan install -
# so a failure anywhere in it stopped a fan service nobody had asked this run
# to touch, ran a handback against a controller it had no business writing to,
# and restored a fan binary that had never been replaced.
#
# The question a rollback has to ask is narrower and is the one below: did
# *this* transaction write, replace, delete, stop, start, enable or disable a
# fan artifact? If it did not, fan control is not part of the rollback, no
# unit of it is stopped, no process of it is scanned for, no safe-state is
# run, and no file of it is restored or removed - which is the same rule
# verify_fan_handback follows on the way in.
rollback_touches_fan_artifacts() {
    local path unit
    for path in ${TXN_MUTATED_PATHS[@]+"${TXN_MUTATED_PATHS[@]}"}; do
        is_fan_artifact_path "$path" && return 0
    done
    for unit in ${TXN_MUTATED_UNITS[@]+"${TXN_MUTATED_UNITS[@]}"}; do
        is_fan_unit "$unit" && return 0
    done
    return 1
}

# fan_path_out_of_scope <path> - a fan file this transaction never changed.
# Putting it back would replace the executable an operator recovers with, in
# the middle of a rollback that has nothing to do with fan control, and the
# bytes it would write are the bytes already there.
fan_path_out_of_scope() {
    is_fan_artifact_path "$1" || return 1
    txn_path_was_mutated "$1" && return 1
    return 0
}

# fan_unit_out_of_scope <unit> - a fan unit whose activity and enablement this
# transaction never changed. Step 4 below "restores" activity by restarting
# every unit that was active at snapshot time, and a restart of a fan service
# this run never stopped is a fan service stopped by the rollback.
fan_unit_out_of_scope() {
    is_fan_unit "$1" || return 1
    txn_unit_was_mutated "$1" && return 1
    return 0
}

# rollback_fan_gate - a fresh quiescence and handback verdict for the rollback
# alone, in the only order that means anything: stop every fan unit that could
# be running, prove no fan writer of any name survived it, and only then ask
# the chip and read the answer back. Asking while a writer is alive is
# answered by whichever of the two wrote last.
rollback_fan_gate() {
    local unit
    warn "this rollback would restore or remove a fan binary or unit; proving"
    warn "the fans are back under the controller before it touches either"
    # Stale by construction: it was earned before install_files() replaced the
    # binary and before a fan service was started again. Clearing it is what
    # makes "nothing below reads it" true rather than merely likely.
    FAN_HANDBACK_VERIFIED=0
    for unit in "${ROLLBACK_FAN_UNITS[@]}"; do
        unit_exists "$unit" || unit_is_active "$unit" || continue
        if ! stop_unit_verified "$unit"; then
            warn "$unit could not be stopped, so it may still be driving the fans"
            return 1
        fi
    done
    # The same lock, in the same place in the same order: after the units are
    # down and before the recheck that the restoration below depends on. A
    # rollback reached without it - a failure early enough in main() that
    # stop_fan_services never ran - takes it here instead, and one that
    # already holds it keeps the one it has.
    if ! fan_lock_acquire; then
        warn "the fan writer lock could not be taken, so a writer could start"
        warn "between this check and the files this rollback would restore"
        return 1
    fi
    if ! wait_for_no_process "fan control" "${ROLLBACK_FAN_BINS[@]}"; then
        warn "a fan writer is still running, so the controller cannot be asked"
        return 1
    fi
    if ! fan_handback_now; then
        warn "the fans could not be confirmed back in the controller's"
        warn "automatic mode"
        return 1
    fi
    return 0
}

rollback() {
    [ "$ROLLBACK_NEEDED" -eq 1 ] || return 0
    [ "$ROLLBACK_RUNNING" -eq 0 ] || return 0
    ROLLBACK_RUNNING=1
    # `set +e` alone does not remove an inherited ERR trap, so a failing
    # restore would re-enter on_error and recurse. Disarm it explicitly.
    #
    # No test can currently make this line matter, and that is not a coverage
    # gap: rollback is only reached as `rollback || true`, and bash suppresses
    # the ERR trap for everything under a command in a `||` list. Change that
    # call shape and this line is the only thing preventing recursion, so it
    # stays.
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

    # 0. The fan gate, before any file is put back and before any unit is
    #    touched. Everything below this point either restores a file or
    #    changes what a unit is doing, and for a fan binary or unit both are
    #    only safe once the chip demonstrably has the fans.
    if rollback_touches_fan_artifacts; then
        if rollback_fan_gate; then
            ROLLBACK_FAN_GATE=1
            ok "the fans are back under the controller; this rollback may restore them"
        else
            ROLLBACK_FAN_GATE=2
            failures=$((failures + 1))
            warn "no fan binary or unit will be restored or removed by this"
            warn "rollback; everything else is still put back"
        fi
    else
        # Nothing of fan control was changed by this transaction, so nothing
        # of it is undone: no unit is stopped, no process is scanned for, no
        # handback is run and no file is restored or removed. A fan service
        # running right now goes on running.
        ok "this transaction changed no fan binary or unit; fan control is not part of this rollback"
    fi

    local unit enabled active
    # 1. Undo enablement and activity this run introduced, before the files
    #    those units point at are replaced.
    while IFS=$'\t' read -r unit enabled active; do
        [ -n "$unit" ] || continue
        if fan_unit_out_of_scope "$unit"; then
            continue
        fi
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
        if fan_path_out_of_scope "$path"; then
            # Not this transaction's file. Its bytes on disk are the bytes
            # that were there when this run started, and replacing them would
            # take the recovery executable away for as long as it takes.
            continue
        fi
        if [ "$ROLLBACK_FAN_GATE" -eq 2 ] && is_fan_artifact_path "$path"; then
            # Left exactly as this install wrote it. An unproven handback
            # means a fan may be parked at a stall duty, and this file is how
            # anyone would get it back.
            ROLLBACK_SKIPPED_FAN+=("$path")
            continue
        fi
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

    # 4. Restore the enablement and activity that existed before. After step
    #    2, never before it: a fan service is only started again once every
    #    fan artifact this rollback intended to restore is back on disk, so it
    #    cannot come up against a binary or a unit file that is still the one
    #    the failed install wrote.
    # Every file this rollback intended to put back is back, so the lock has
    # done its work - and it has to come off before the loop below, which
    # starts services. A fan service started while this still held the lock
    # would be refused at its own and fail to come up.
    fan_lock_release
    while IFS=$'\t' read -r unit enabled active; do
        [ -n "$unit" ] || continue
        if fan_unit_out_of_scope "$unit"; then
            continue
        fi
        if [ "$ROLLBACK_FAN_GATE" -eq 2 ] && is_fan_unit "$unit"; then
            warn "not enabling or starting $unit: its binary and unit file were"
            warn "left as this install wrote them"
            continue
        fi
        if [ "$enabled" -eq 1 ] && ! unit_is_enabled "$unit"; then
            systemctl enable "$unit" >/dev/null 2>&1 || failures=$((failures + 1))
        fi
        # restart, not start: a unit that stayed active through the failure is
        # still running the code that was just rolled back underneath it.
        if [ "$active" -eq 1 ]; then
            systemctl restart "$unit" >/dev/null 2>&1 || failures=$((failures + 1))
        fi
    done < "$TXN_DIR/units.tsv"

    local skipped
    if [ "${#ROLLBACK_SKIPPED_FAN[@]}" -gt 0 ]; then
        warn "INCOMPLETE ROLLBACK: ${#ROLLBACK_SKIPPED_FAN[@]} fan path(s) were"
        warn "left exactly as this install wrote them:"
        for skipped in "${ROLLBACK_SKIPPED_FAN[@]}"; do
            warn "  $skipped"
        done
        printf '%s\n' "${ROLLBACK_SKIPPED_FAN[@]}" \
            > "$TXN_DIR/fan-rollback-skipped" 2>/dev/null || true
        warn "recorded in $TXN_DIR/fan-rollback-skipped"
        fan_handback_hint
    fi

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
    txn_touch_path "$BIN_DIR/qnap-tsx70-lcd"
    ok "installed $BIN_DIR/qnap-tsx70-lcd"

    if [ -f "$CONF_FILE" ]; then
        ok "kept existing $CONF_FILE (see config/qnap-tsx70-lcd.conf.example for new keys)"
    else
        run install -m 0644 "$REPO_ROOT/config/qnap-tsx70-lcd.conf.example" "$CONF_FILE"
        txn_touch_path "$CONF_FILE"
        ok "installed $CONF_FILE"
    fi

    run install -m 0644 "$REPO_ROOT/systemd/qnap-tsx70-lcd.service" \
        "$UNIT_DIR/$NEW_LCD_UNIT"
    txn_touch_path "$UNIT_DIR/$NEW_LCD_UNIT"
    ok "installed $NEW_LCD_UNIT"

    if [ "$WITH_FAN" -eq 1 ]; then
        run install -m 0755 "$REPO_ROOT/bin/qnap-tsx70-fancontrol" \
            "$BIN_DIR/qnap-tsx70-fancontrol"
        txn_touch_path "$BIN_DIR/qnap-tsx70-fancontrol"
        run install -m 0644 "$REPO_ROOT/systemd/qnap-tsx70-fancontrol.service" \
            "$UNIT_DIR/$NEW_FAN_UNIT"
        txn_touch_path "$UNIT_DIR/$NEW_FAN_UNIT"
        txn_mkdir "$(dirname "$MODULES_FILE")" 0755
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '       [dry-run] write coretemp and f71882fg to %s\n' \
                "$MODULES_FILE"
        else
            printf 'coretemp\nf71882fg\n' > "$MODULES_FILE"
            chmod 0644 "$MODULES_FILE"
        fi
        txn_touch_path "$MODULES_FILE"
        run modprobe f71882fg 2>/dev/null || warn "modprobe f71882fg failed"
        run modprobe coretemp 2>/dev/null || true
        ok "installed fan control (experimental)"
    fi
    # The two `install` calls above are the whole reason this run holds the
    # canonical writer lock: they replace the binary and the unit file that a
    # `qnap-tsx70-fancontrol run` or `calibrate` executes, and the lock is
    # what stopped one from starting between the quiescence recheck and them.
    # Those files are now final, so the lock comes off here rather than later.
    #
    # Later would be wrong, not merely generous: enable_services() calls
    # restore_fan_activity(), which starts a fan writer, and a writer started
    # while this script still held the lock would be refused at its own and
    # fail to come up. And later would buy nothing - what remove_legacy()
    # deletes is saturn-* artifacts, which are driven by a program that
    # predates this lock and never takes it. Their protection is the process
    # gate and the PID-file handling, which is where it has to be.
    fan_lock_release
}

enable_services() {
    step "Enabling services"
    run systemctl daemon-reload
    run systemctl enable "$NEW_LCD_UNIT"
    run systemctl restart "$NEW_LCD_UNIT"
    txn_touch_unit "$NEW_LCD_UNIT"
    ok "qnap-tsx70-lcd enabled and started"

    if [ "$WITH_FAN" -eq 1 ]; then
        # Enabling a fan service that has no calibration means the next boot
        # starts it, it exits, and systemd burns its start limit. So the unit
        # is enabled only when a cache is already there and validates.
        # cache_is_valid only reads, so the decision is reported in a dry
        # run too rather than being silently skipped.
        if cache_is_valid "$CACHE_FILE"; then
            run systemctl enable "$NEW_FAN_UNIT"
            txn_touch_unit "$NEW_FAN_UNIT"
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
        # Safe here only because install_files() released the writer lock on
        # its way out: this starts a fan writer, and a writer started while
        # this run still held the lock would be refused at its own and fail to
        # come up.
        restore_fan_activity
    fi
}

# restore_fan_activity - a fan service that was running when this install
# started was stopped so its binary could be replaced. Leaving it down is a
# change to the machine nobody asked for, so it goes back up. Without a valid
# calibration it cannot: starting it would fail at once and burn the unit's
# start limit, so that case is reported instead of guessed at.
#
# Ordering: this runs after install_files(), which is where the fan binary and
# unit reach their final bytes and where the writer lock comes off. A service
# started before either would be running the old binary, or would be refused
# at a lock this script was still holding.
restore_fan_activity() {
    [ "$FAN_UNIT_WAS_ACTIVE" -eq 1 ] || return 0
    if cache_is_valid "$CACHE_FILE"; then
        run systemctl restart "$NEW_FAN_UNIT"
        txn_touch_unit "$NEW_FAN_UNIT"
        ok "restarted $NEW_FAN_UNIT: it was active before this install"
    else
        warn "$NEW_FAN_UNIT was active before this install and was stopped to"
        warn "replace its binary, but $CACHE_FILE does not validate, so it was"
        warn "NOT restarted. Calibrate, then start it while you are watching:"
        warn "  qnap-tsx70-fancontrol calibrate --yes"
        warn "  systemctl start qnap-tsx70-fancontrol"
    fi
}

# report_fan_state - say what the fan service is actually doing, not what a
# default install would be doing.
#
# This line used to be an unconditional "Fan control is installed but NOT
# running", which is true of a fresh --with-fan-control install and false of
# the case directly above it: a reinstall over a machine whose fan service was
# already calibrated and active, which stop_fan_services() stopped so the
# binary could be replaced and restore_fan_activity() has just started again.
# Telling that operator their fans are unmanaged is worse than saying nothing
# - it is the sentence that makes them go and start a second writer.
#
# Every query here is best-effort on purpose. The install has already
# succeeded by this point and `set -e` is still armed, so a systemctl that
# answers "disabled" with exit 1 - which is what it does - must not turn a
# finished install into a failed one. Hence the `|| true` shape and no
# unguarded command substitutions.
report_fan_state() {
    local active=0 enabled=0
    unit_is_active "$NEW_FAN_UNIT" && active=1
    unit_is_enabled "$NEW_FAN_UNIT" && enabled=1

    if [ "$active" -eq 1 ]; then
        ok "Fan control is installed and RUNNING ($NEW_FAN_UNIT is active)."
        if [ "$enabled" -eq 1 ]; then
            info "It is enabled, so it starts at boot too."
        else
            info "It is NOT enabled, so it will not start at the next boot:"
            info "  systemctl enable qnap-tsx70-fancontrol"
        fi
        info "Watch it: journalctl -u qnap-tsx70-fancontrol -f"
        info "Stop it and hand the fans back: systemctl stop qnap-tsx70-fancontrol"
        return 0
    fi

    warn "Fan control is installed but NOT running."
    if [ "$enabled" -eq 1 ]; then
        info "It is enabled, so it will start at the next boot."
        info "Start it now while you are watching:"
        info "  systemctl start qnap-tsx70-fancontrol"
        return 0
    fi
    info "It needs a calibration run that deliberately stalls the fan."
    info "Read docs/FAN_CONTROL.md first, then:"
    info "  qnap-tsx70-fancontrol status"
    info "  qnap-tsx70-fancontrol calibrate --yes"
    info "  systemctl enable --now qnap-tsx70-fancontrol"
    return 0
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

    local legacy=0
    if [ "$MIGRATE" -eq 1 ] && detect_legacy; then
        legacy=1
    fi
    # Before preflight: the scope is what decides whether a legacy LCD holding
    # the serial port is a planned transition or an unresolvable conflict.
    plan_scope "$legacy"

    preflight
    install_deps

    if [ "$legacy" -eq 1 ]; then
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
    # Here, and not earlier: stop_current_services is what proves every
    # in-scope fan writer is gone, and asking the chip to take the fans back
    # while one is still driving them would be answered by whichever of the
    # two wrote last. Here, and not later: install_files replaces the current
    # fan binary and remove_legacy deletes the legacy one, and those are the
    # files this is protecting.
    if ! verify_fan_handback; then
        on_error 1
    fi
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
        report_fan_state
    fi
    if [ "$legacy" -eq 1 ]; then
        info ""
        info "Legacy saturn-* artifacts were migrated and removed."
        if [ "$CACHE_PRESERVED_INVALID" -eq 1 ]; then
            info "The legacy calibration cache did NOT validate, so it was kept at"
            info "  $LEGACY_CACHE"
        elif [ "$CACHE_SUPERSEDED" -eq 1 ]; then
            info "A newer calibration was already in place, so the legacy cache"
            info "was not migrated and was kept at"
            info "  $LEGACY_CACHE"
        fi
        info "Backup: $BACKUP_DIR (see docs/MIGRATION_FROM_SATURN.md)"
    fi
}

main "$@"
