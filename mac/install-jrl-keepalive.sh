#!/bin/bash
# JRL Messages: one-command Mac keepalive install.
#
#   bash install-jrl-keepalive.sh                 install with defaults
#   bash install-jrl-keepalive.sh --with-power    also apply pmset (asks sudo)
#   bash install-jrl-keepalive.sh --daily-restart opt in to a local 4:15 restart
#   bash install-jrl-keepalive.sh --restart-hour 5 --restart-minute 30
#   bash install-jrl-keepalive.sh --no-daily-restart
#
# What it installs, all user-level and reversible with the uninstaller:
#   1. Copies jrl-keepalive.sh to ~/Library/Application Support/jrl-keepalive
#   2. A launchd job that runs it every 2 minutes (relaunch Messages or
#      BlueBubbles if they die, then verify Messages accepts an Apple event)
#   3. Optionally, a launchd job that restarts Messages once a day. It is off
#      by default because only the Windows agent can fence its durable outbox.
#   4. Requests App Nap opt-out where macOS permits that preference write
#   5. Verifies a launchd-context tick; when daily restart is explicitly
#      enabled, also verifies a real restart rather than trusting open(1)

set -u

RESTART_HOUR=4
RESTART_MINUTE=15
DAILY_RESTART=0
WITH_POWER=0

while [ $# -gt 0 ]; do
    case "$1" in
        --with-power) WITH_POWER=1 ;;
        --daily-restart) DAILY_RESTART=1 ;;
        --no-daily-restart) DAILY_RESTART=0 ;;
        --restart-hour)
            [ "$#" -ge 2 ] || { echo "--restart-hour requires a value"; exit 2; }
            shift
            RESTART_HOUR="$1"
            ;;
        --restart-minute)
            [ "$#" -ge 2 ] || { echo "--restart-minute requires a value"; exit 2; }
            shift
            RESTART_MINUTE="$1"
            ;;
        *) echo "Unknown option: $1"; exit 2 ;;
    esac
    shift
done

case "$RESTART_HOUR" in
    ''|*[!0-9]*) echo "--restart-hour must be an integer from 0 to 23"; exit 2 ;;
esac
case "$RESTART_MINUTE" in
    ''|*[!0-9]*) echo "--restart-minute must be an integer from 0 to 59"; exit 2 ;;
esac
RESTART_HOUR=$((10#$RESTART_HOUR))
RESTART_MINUTE=$((10#$RESTART_MINUTE))
if [ "$RESTART_HOUR" -lt 0 ] || [ "$RESTART_HOUR" -gt 23 ]; then
    echo "--restart-hour must be from 0 to 23"
    exit 2
fi
if [ "$RESTART_MINUTE" -lt 0 ] || [ "$RESTART_MINUTE" -gt 59 ]; then
    echo "--restart-minute must be from 0 to 59"
    exit 2
fi

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/jrl-keepalive"
AGENTS="$HOME/Library/LaunchAgents"
LOG="$HOME/Library/Logs/jrl-keepalive.log"
SCRIPT="$SUPPORT/jrl-keepalive.sh"
UID_NUM="$(id -u)"
KEEPALIVE_LABEL="com.jrl.messages.keepalive"
RESTART_LABEL="com.jrl.messages.dailyrestart"
KEEPALIVE_PLIST="$AGENTS/$KEEPALIVE_LABEL.plist"
RESTART_PLIST="$AGENTS/$RESTART_LABEL.plist"
LAST_TICK="$SUPPORT/last-successful-tick"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

echo "== JRL Messages Mac keepalive installer =="

mkdir -p "$SUPPORT" "$AGENTS" "$HOME/Library/Logs" || fail "could not create support folders"

# Stop loaded jobs before replacing the script they execute.
for label in "$KEEPALIVE_LABEL" "$RESTART_LABEL"; do
    launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1 || true
    launchctl unload "$AGENTS/$label.plist" >/dev/null 2>&1 || true
done
cp "$SELF_DIR/jrl-keepalive.sh" "$SCRIPT.new.$$" \
    || fail "could not stage keepalive script"
chmod +x "$SCRIPT.new.$$" || fail "could not make keepalive executable"
mv -f "$SCRIPT.new.$$" "$SCRIPT" || fail "could not install keepalive script"
echo "1) Keepalive script installed at: $SCRIPT"

# ---- App Nap is an optional optimization. On current macOS releases,
# Messages may resolve to the protected com.apple.MobileSMS preference domain.
# A denied defaults write must not prevent the actual launchd watchdog, power
# settings, or unattended Automation verification from being installed.
echo "2) App Nap preference checks:"
MESSAGES_ID="$(osascript -e 'id of app "Messages"' 2>/dev/null || true)"
if [ -n "$MESSAGES_ID" ] \
        && defaults write "$MESSAGES_ID" NSAppSleepDisabled -bool YES \
            >/dev/null 2>&1 \
        && defaults read "$MESSAGES_ID" NSAppSleepDisabled \
            >/dev/null 2>&1; then
    echo "   Messages: App Nap opt-out applied ($MESSAGES_ID)"
else
    echo "   WARNING: macOS would not allow the Messages App Nap preference."
    echo "   Continuing safely; --with-power and BlueBubbles' Keep macOS"
    echo "   Awake setting provide the important sleep protection."
fi
BB_ID="$(osascript -e 'id of app "BlueBubbles Server"' 2>/dev/null \
      || osascript -e 'id of app "BlueBubbles"' 2>/dev/null || true)"
if [ -n "${BB_ID:-}" ]; then
    if defaults write "$BB_ID" NSAppSleepDisabled -bool YES \
            >/dev/null 2>&1 \
            && defaults read "$BB_ID" NSAppSleepDisabled \
            >/dev/null 2>&1; then
        echo "   BlueBubbles: App Nap opt-out applied ($BB_ID)"
    else
        echo "   WARNING: macOS would not allow the BlueBubbles App Nap"
        echo "   preference. Continuing; enable Keep macOS Awake in BlueBubbles."
    fi
else
    echo "   WARNING: BlueBubbles was not found; install it, enable Keep"
    echo "   macOS Awake, and rerun this installer."
fi

load_agent() {
    local plist="$1" label="$2"
    launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1
    launchctl unload "$plist" >/dev/null 2>&1
    if launchctl bootstrap "gui/$UID_NUM" "$plist" >/dev/null 2>&1; then
        echo "   loaded: $label"
        return 0
    elif launchctl load -w "$plist" >/dev/null 2>&1; then
        echo "   loaded (legacy): $label"
        return 0
    else
        echo "   ERROR: could not load $label"
        return 1
    fi
}

# Trigger the foreground Automation prompt while both old jobs are stopped,
# so a RunAtLoad tick cannot steal the shared lock and make this test a no-op.
echo "3) Testing Messages control from this Terminal. If macOS asks whether"
echo "   Terminal or bash may control Messages, click Allow."
if ! bash "$SCRIPT" --tick; then
    fail "foreground keepalive test failed; approve Automation and rerun"
fi

# ---- every-2-minutes keepalive
cat > "$KEEPALIVE_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.jrl.messages.keepalive</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT</string>
    </array>
    <key>StartInterval</key><integer>120</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
plutil -lint "$KEEPALIVE_PLIST" >/dev/null \
    || fail "generated keepalive plist did not pass plutil"
echo "4) Keepalive runs every 2 minutes:"
load_agent "$KEEPALIVE_PLIST" "$KEEPALIVE_LABEL" \
    || fail "could not load $KEEPALIVE_LABEL"

# ---- daily Messages restart (the scheduled Wake Mac)
if [ "$DAILY_RESTART" = "1" ]; then
    cat > "$RESTART_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.jrl.messages.dailyrestart</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT</string>
        <string>--restart-messages</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>$RESTART_HOUR</integer>
        <key>Minute</key><integer>$RESTART_MINUTE</integer>
    </dict>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
    plutil -lint "$RESTART_PLIST" >/dev/null \
        || fail "generated restart plist did not pass plutil"
    printf "5) Daily Messages restart at %02d:%02d:\n" "$RESTART_HOUR" "$RESTART_MINUTE"
    load_agent "$RESTART_PLIST" "$RESTART_LABEL" \
        || fail "could not load $RESTART_LABEL"
else
    launchctl bootout "gui/$UID_NUM/$RESTART_LABEL" >/dev/null 2>&1 || true
    launchctl unload "$RESTART_PLIST" >/dev/null 2>&1 || true
    rm -f "$RESTART_PLIST"
    echo "5) Daily restart: off (safe default; use --daily-restart to opt in)"
fi

# A Terminal test does not prove that the LaunchAgent has the same Automation
# access. Remove the marker, kickstart that exact job, and wait for a fresh one.
echo "6) Verifying the installed launchd job (not just this Terminal)..."
rm -f "$LAST_TICK"
launchctl kickstart -k "gui/$UID_NUM/$KEEPALIVE_LABEL" >/dev/null 2>&1 \
    || fail "could not kickstart $KEEPALIVE_LABEL"
VERIFY_LEFT=60
while [ "$VERIFY_LEFT" -gt 0 ] && [ ! -s "$LAST_TICK" ]; do
    sleep 1
    VERIFY_LEFT=$((VERIFY_LEFT - 1))
done
[ -s "$LAST_TICK" ] \
    || fail "launchd keepalive did not write a success marker; run --status"
echo "   launchd-context tick verified."

if [ "$DAILY_RESTART" = "1" ]; then
    echo "   Verifying Wake behavior with one controlled Messages restart now..."
    bash "$SCRIPT" --restart-messages \
        || fail "Messages did not complete a verified quit/relaunch"
fi

# ---- power settings
echo
if [ "$WITH_POWER" = "1" ]; then
    echo "7) Applying power settings (sudo will prompt):"
    sudo pmset -a sleep 0 disksleep 0 displaysleep 10 \
        || fail "could not apply sleep settings"
    sudo pmset -a womp 1 autorestart 1 \
        || fail "could not apply wake/restart settings"
    if [ "$DAILY_RESTART" = "1" ]; then
        WAKE_TOTAL=$((RESTART_HOUR * 60 + RESTART_MINUTE - 5))
        if [ "$WAKE_TOTAL" -lt 0 ]; then
            WAKE_TOTAL=$((WAKE_TOTAL + 1440))
        fi
        WAKE_HOUR=$((WAKE_TOTAL / 60))
        WAKE_MINUTE=$((WAKE_TOTAL % 60))
        sudo pmset repeat wakeorpoweron MTWRFSU \
            "$(printf '%02d:%02d:00' "$WAKE_HOUR" "$WAKE_MINUTE")" \
            || fail "could not schedule the daily pre-wake"
    fi
    echo "   Done: system sleep off, display may sleep, wake-on-network on,"
    if [ "$DAILY_RESTART" = "1" ]; then
        echo "   auto-restart after power failure on, daily pre-restart wake set."
    else
        echo "   auto-restart after power failure on; no repeating wake changed."
    fi
else
    echo "7) Power settings NOT changed. Recommended once (asks for sudo):"
    echo "     sudo pmset -a sleep 0 disksleep 0 displaysleep 10"
    echo "     sudo pmset -a womp 1 autorestart 1"
    echo "   Or rerun this installer with --with-power"
fi

echo
echo "== Done. Verify anytime with: bash \"$SCRIPT\" --status =="
