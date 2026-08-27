#!/bin/bash
# JRL Messages Mac relay watchdog.
#
# The frequent job verifies that Messages and the BlueBubbles application are
# alive and performs a small AppleScript liveness probe.  The daily job makes
# the same Messages restart as Wake Mac, but does not claim success until the
# old PID is gone and a different PID is running.  The jobs share one lock so
# a liveness tick can never overlap a restart.

set -u

SUPPORT_DIR="$HOME/Library/Application Support/jrl-keepalive"
LOG="$HOME/Library/Logs/jrl-keepalive.log"
LOCK_DIR="$SUPPORT_DIR/run.lock"
LAST_TICK="$SUPPORT_DIR/last-successful-tick"
LAST_RESTART="$SUPPORT_DIR/last-successful-restart"
ACTION="tick"

mkdir -p "$SUPPORT_DIR" "$(dirname "$LOG")"

timestamp() {
    date '+%Y-%m-%d %H:%M:%S %z'
}

log() {
    printf '%s %s\n' "$(timestamp)" "$*" >> "$LOG"
}

record_error() {
    printf '%s %s\n' "$(timestamp)" "$*" \
        > "$SUPPORT_DIR/last-$ACTION-error"
    log "ERROR: $*"
}

record_success() {
    local marker="$1"
    local message="$2"
    printf '%s %s\n' "$(timestamp)" "$message" > "$marker"
    rm -f "$SUPPORT_DIR/last-$ACTION-error"
    log "$message"
}

rotate_log() {
    local bytes=0
    if [ -f "$LOG" ]; then
        bytes="$(stat -f%z "$LOG" 2>/dev/null || printf '0')"
    fi
    case "$bytes" in
        ''|*[!0-9]*) bytes=0 ;;
    esac
    if [ "$bytes" -gt 1048576 ]; then
        mv -f "$LOG" "$LOG.old"
    fi
}

release_lock() {
    local owner=""
    owner="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ "$owner" = "$$" ]; then
        rm -f "$LOCK_DIR/pid" 2>/dev/null || true
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
}

install_lock_traps() {
    trap release_lock EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

acquire_lock() {
    local wait_left="${1:-0}"
    local announced=0
    local owner=""
    local stale_lock=""
    while :; do
        if mkdir "$LOCK_DIR" 2>/dev/null; then
            if ! printf '%s\n' "$$" > "$LOCK_DIR/pid"; then
                rmdir "$LOCK_DIR" 2>/dev/null || true
                record_error "could not record watchdog lock ownership"
                return 1
            fi
            install_lock_traps
            return 0
        fi

        owner="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)"
        if [ -z "$owner" ]; then
            # Give a just-created owner a bounded moment to publish its PID.
            sleep 1
            owner="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)"
        fi
        if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
            if [ "$wait_left" -le 0 ]; then
                log "another watchdog action is already running (pid $owner); skipped"
                return 1
            fi
            if [ "$announced" -eq 0 ]; then
                log "waiting for watchdog action pid $owner before $ACTION"
                announced=1
            fi
            sleep 1
            wait_left=$((wait_left - 1))
            continue
        fi

        # Atomically quarantine a stale lock. Only one contender can rename
        # this exact directory; nobody can delete a newly acquired owner's
        # PID during the stale-recovery race.
        stale_lock="$SUPPORT_DIR/run.lock.stale.$$"
        if mv "$LOCK_DIR" "$stale_lock" 2>/dev/null; then
            rm -f "$stale_lock/pid" 2>/dev/null || true
            rmdir "$stale_lock" 2>/dev/null || true
            continue
        fi
        sleep 1
    done
}

messages_pid() {
    pgrep -x "Messages" 2>/dev/null | sed -n '1p'
}

bluebubbles_app() {
    if [ -d "/Applications/BlueBubbles Server.app" ]; then
        printf '%s\n' "BlueBubbles Server"
    elif [ -d "/Applications/BlueBubbles.app" ]; then
        printf '%s\n' "BlueBubbles"
    elif [ -d "$HOME/Applications/BlueBubbles Server.app" ]; then
        printf '%s\n' "BlueBubbles Server"
    elif [ -d "$HOME/Applications/BlueBubbles.app" ]; then
        printf '%s\n' "BlueBubbles"
    else
        printf '%s\n' ""
    fi
}

bluebubbles_running() {
    pgrep -x "BlueBubbles Server" >/dev/null 2>&1 \
        || pgrep -x "BlueBubbles" >/dev/null 2>&1
}

wait_for_messages() {
    local excluded_pid="${1:-}"
    local attempts="${2:-30}"
    local pid=""
    while [ "$attempts" -gt 0 ]; do
        pid="$(messages_pid)"
        if [ -n "$pid" ] && { [ -z "$excluded_pid" ] \
                || [ "$pid" != "$excluded_pid" ]; }; then
            printf '%s\n' "$pid"
            return 0
        fi
        sleep 1
        attempts=$((attempts - 1))
    done
    return 1
}

wait_for_exit() {
    local pid="$1"
    local attempts="$2"
    while [ "$attempts" -gt 0 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        sleep 1
        attempts=$((attempts - 1))
    done
    return 1
}

ensure_messages() {
    if [ -n "$(messages_pid)" ]; then
        return 0
    fi
    log "Messages is not running; launching it"
    if ! open -gja "Messages" >/dev/null 2>&1; then
        record_error "could not ask macOS to launch Messages"
        return 1
    fi
    if ! wait_for_messages "" 25 >/dev/null; then
        record_error "Messages did not become ready after launch"
        return 1
    fi
    log "Messages launch verified"
}

ensure_bluebubbles() {
    local app=""
    app="$(bluebubbles_app)"
    if [ -z "$app" ]; then
        record_error "BlueBubbles app was not found in /Applications or ~/Applications"
        return 1
    fi
    if bluebubbles_running; then
        return 0
    fi
    log "$app is not running; launching it"
    if ! open -gja "$app" >/dev/null 2>&1; then
        record_error "could not ask macOS to launch $app"
        return 1
    fi
    local attempts=25
    while [ "$attempts" -gt 0 ]; do
        if bluebubbles_running; then
            log "$app launch verified"
            return 0
        fi
        sleep 1
        attempts=$((attempts - 1))
    done
    record_error "$app did not become ready after launch"
    return 1
}

nudge_messages() {
    # This proves the launchd process can reach Messages through Automation.
    # It is a liveness probe, not a promise that Apple has released every row.
    if /usr/bin/osascript \
        -e 'with timeout of 10 seconds' \
        -e 'tell application "Messages" to count of windows' \
        -e 'end timeout' >/dev/null 2>&1; then
        record_success "$LAST_TICK" \
            "tick verified: Messages and BlueBubbles are running; Messages responded"
        return 0
    fi
    record_error "Messages Automation probe failed; check Privacy & Security > Automation"
    return 1
}

restart_messages() {
    local old_pid=""
    local new_pid=""
    old_pid="$(messages_pid)"

    if [ -n "$old_pid" ]; then
        log "restart: requesting Messages quit (old pid $old_pid)"
        if ! /usr/bin/osascript \
                -e 'with timeout of 20 seconds' \
                -e 'quit app "Messages"' \
                -e 'end timeout' >/dev/null 2>&1; then
            # Automation denial is not proof that Messages is hung. Never
            # turn a permission failure into a force-quit that can interrupt
            # an outgoing message.
            record_error "Messages quit request failed; no force was used. Check Privacy & Security > Automation"
            return 1
        fi
        if ! wait_for_exit "$old_pid" 22; then
            log "restart: old Messages pid $old_pid did not exit; sending TERM"
            kill -TERM "$old_pid" 2>/dev/null || true
            if ! wait_for_exit "$old_pid" 12; then
                record_error "Messages pid $old_pid would not exit; restart aborted"
                return 1
            fi
        fi
        log "restart: old Messages pid $old_pid exited"
    else
        log "restart: Messages was not running"
    fi

    if ! open -gja "Messages" >/dev/null 2>&1; then
        record_error "macOS rejected the Messages relaunch request"
        return 1
    fi
    if ! new_pid="$(wait_for_messages "$old_pid" 30)"; then
        record_error "Messages did not relaunch with a new process"
        return 1
    fi

    # Require a real AppleEvent response from the new process before success.
    if ! /usr/bin/osascript \
            -e 'with timeout of 15 seconds' \
            -e 'tell application "Messages" to count of windows' \
            -e 'end timeout' >/dev/null 2>&1; then
        record_error "new Messages pid $new_pid launched but failed its Automation probe"
        return 1
    fi
    record_success "$LAST_RESTART" \
        "restart verified: Messages pid $old_pid -> $new_pid"
}

status() {
    local mpid=""
    mpid="$(messages_pid)"
    printf '%s\n' "== jrl-keepalive status =="
    if [ -n "$mpid" ]; then
        printf '%s\n' "Messages running:     yes (pid $mpid)"
    else
        printf '%s\n' "Messages running:     NO"
    fi
    if bluebubbles_running; then
        printf '%s\n' "BlueBubbles running:  yes"
    else
        printf '%s\n' "BlueBubbles running:  NO"
    fi
    printf '%s\n' "launchd jobs:"
    launchctl list 2>/dev/null | grep -i "com.jrl.messages" \
        || printf '%s\n' "  (none loaded)"
    printf '%s\n' "Last verified tick:"
    sed 's/^/  /' "$LAST_TICK" 2>/dev/null || printf '%s\n' "  (none yet)"
    printf '%s\n' "Last verified restart:"
    sed 's/^/  /' "$LAST_RESTART" 2>/dev/null || printf '%s\n' "  (none yet)"
    printf '%s\n' "Unresolved tick error:"
    sed 's/^/  /' "$SUPPORT_DIR/last-tick-error" 2>/dev/null \
        || printf '%s\n' "  (none)"
    printf '%s\n' "Unresolved restart error:"
    sed 's/^/  /' "$SUPPORT_DIR/last-restart-error" 2>/dev/null \
        || printf '%s\n' "  (none)"
    printf '%s\n' "Recent log:"
    tail -n 12 "$LOG" 2>/dev/null || printf '%s\n' "  (no log yet)"
}

rotate_log
case "${1:-}" in
    --restart-messages)
        ACTION="restart"
        acquire_lock 90 || { record_error "restart could not acquire watchdog lock"; exit 1; }
        ensure_bluebubbles || exit 1
        restart_messages
        ;;
    --status)
        status
        ;;
    --tick|"")
        ACTION="tick"
        acquire_lock 0 || exit 0
        ensure_messages || exit 1
        ensure_bluebubbles || exit 1
        nudge_messages
        ;;
    *)
        printf '%s\n' "Usage: $0 [--tick|--restart-messages|--status]" >&2
        exit 2
        ;;
esac
