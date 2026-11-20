#!/bin/bash
# One seat check + auto-reserve. Run by launchd every 10 min (see install.sh). Rotates the log at 5 MB.
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LOG=/tmp/awsevents_cron.log
MAX_BYTES=5242880
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt "$MAX_BYTES" ]; then
  mv -f "$LOG.1" "$LOG.2" 2>/dev/null
  mv -f "$LOG" "$LOG.1"
fi
exec >> "$LOG" 2>&1
# /tmp is purged by macOS; keep the dashboard page present for serve-dashboard.sh
cp -f "$REPO/scripts/registration_report.html" /tmp/registration_report.html
# After wake from sleep DNS is often not up yet; wait (bounded) instead of failing the check.
for _ in $(seq 1 12); do
  /usr/bin/dscacheutil -q host -a name catalog.awsevents.com 2>/dev/null | grep -q ip_address && break
  sleep 5
done
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
cd "$REPO" && exec "$UV" run awsevents seats --auto-reserve
