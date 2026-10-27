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
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
cd "$REPO" && exec "$UV" run awsevents seats --auto-reserve
