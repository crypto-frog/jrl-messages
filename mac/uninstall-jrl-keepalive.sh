#!/bin/bash
# Removes everything install-jrl-keepalive.sh added. Power settings are
# left alone (they are printed below so you can revert them by hand).
set -u
AGENTS="$HOME/Library/LaunchAgents"
SUPPORT="$HOME/Library/Application Support/jrl-keepalive"
UID_NUM="$(id -u)"

for label in com.jrl.messages.keepalive com.jrl.messages.dailyrestart; do
    launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1
    launchctl unload "$AGENTS/$label.plist" >/dev/null 2>&1
    rm -f "$AGENTS/$label.plist"
    echo "removed: $label"
done

rm -rf "$SUPPORT"
echo "removed: $SUPPORT"

MESSAGES_ID="$(osascript -e 'id of app "Messages"' 2>/dev/null || true)"
if [ -n "${MESSAGES_ID:-}" ]; then
    defaults delete "$MESSAGES_ID" NSAppSleepDisabled >/dev/null 2>&1
fi
BB_ID="$(osascript -e 'id of app "BlueBubbles Server"' 2>/dev/null \
      || osascript -e 'id of app "BlueBubbles"' 2>/dev/null || true)"
if [ -n "${BB_ID:-}" ]; then
    defaults delete "$BB_ID" NSAppSleepDisabled >/dev/null 2>&1
fi
echo "App Nap settings restored to defaults."

echo
echo "Power settings were not touched. To revert the recommended ones:"
echo "  sudo pmset -a sleep 1 displaysleep 10"
echo "  sudo pmset repeat cancel"
echo "Done."
