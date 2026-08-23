#!/bin/bash
# Health-check watchdog: curls the bot's own /v1/healthz every minute (via
# the companion systemd timer) and restarts the vera-bot service if it's
# unresponsive or returns a non-200. Cheap insurance against the judge
# harness's "3 consecutive failed health checks disqualify the run" rule --
# this catches a hung/crashed process before the harness's own checks do.
set -u

URL="http://127.0.0.1:8000/v1/healthz"
STATE_FILE="/home/ubuntu/vera-bot/.watchdog_fail_count"
MAX_FAILS=3

status=$(curl -s -o /dev/null -w "%{http_code}" -m 10 "$URL" 2>/dev/null || echo "000")

if [ "$status" = "200" ]; then
    echo 0 > "$STATE_FILE"
    exit 0
fi

fails=0
[ -f "$STATE_FILE" ] && fails=$(cat "$STATE_FILE")
fails=$((fails + 1))
echo "$fails" > "$STATE_FILE"

logger -t vera-bot-watchdog "healthz check failed (status=$status), consecutive failures=$fails"

if [ "$fails" -ge "$MAX_FAILS" ]; then
    logger -t vera-bot-watchdog "restarting vera-bot after $fails consecutive failures"
    systemctl restart vera-bot
    echo 0 > "$STATE_FILE"
fi
