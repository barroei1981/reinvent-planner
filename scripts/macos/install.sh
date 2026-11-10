#!/bin/bash
# Installs two per-user launchd agents: seat check every 10 min, and the dashboard server (KeepAlive).
# Runs in your login session (needed to read Chrome's cookies). Re-run safe; remove with --remove.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
SEATS=com.roeibar.awsevents-seats
DASH=com.roeibar.awsevents-dashboard

unload() { launchctl bootout "$DOMAIN/$1" 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  for l in $SEATS $DASH; do unload "$l"; rm -f "$AGENTS/$l.plist"; done
  echo "Removed $SEATS and $DASH"; exit 0
fi

# plist <label> <script> <log> <extra-keys>
plist() {
  cat > "$AGENTS/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$2</string></array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$3</string>
  <key>StandardErrorPath</key><string>$3</string>
  $4
</dict>
</plist>
PLIST
  plutil -lint "$AGENTS/$1.plist" >/dev/null
  unload "$1"
  launchctl bootstrap "$DOMAIN" "$AGENTS/$1.plist"
}

mkdir -p "$AGENTS"
chmod +x "$DIR/run-seats.sh" "$DIR/serve-dashboard.sh"
plist $SEATS "$DIR/run-seats.sh" /tmp/awsevents_cron.log '<key>StartInterval</key><integer>600</integer>'
plist $DASH "$DIR/serve-dashboard.sh" /tmp/awsevents_dashboard.log '<key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>'
echo "Installed $SEATS (every 10 min) and $DASH (http://localhost:8000/registration_report.html)"
