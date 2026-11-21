#!/bin/bash
# Serves the seat dashboard from /tmp on loopback (static files + the report's re-login action). Idempotent: refreshes the report, clears a stale listener.
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PORT=8000
cp -f "$REPO/scripts/registration_report.html" /tmp/registration_report.html
stale=$(lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null)
# shellcheck disable=SC2086  # $stale may hold several PIDs
if [ -n "$stale" ]; then
  kill $stale 2>/dev/null; sleep 2
  kill -9 $stale 2>/dev/null
fi
cd "$REPO" && exec /usr/bin/python3 -m agent.dashboard_server --port $PORT --directory /tmp
