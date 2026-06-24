#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_SCRIPT="$SCRIPT_DIR/run_sync_once.sh"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/sync_cron.log"
TMP_FILE="$(mktemp)"

mkdir -p "$LOG_DIR"

CRON_CMD="cd $PROJECT_DIR && /bin/sh $RUN_SCRIPT >> $LOG_FILE 2>&1"
CRON_LINE="*/5 * * * * $CRON_CMD"

if crontab -l >/dev/null 2>&1; then
  crontab -l | grep -Fv "$RUN_SCRIPT" > "$TMP_FILE" || true
else
  : > "$TMP_FILE"
fi

printf '%s\n' "$CRON_LINE" >> "$TMP_FILE"
crontab "$TMP_FILE"
rm -f "$TMP_FILE"

echo "Installed cron job:"
echo "$CRON_LINE"
