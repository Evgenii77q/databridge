#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" -m receipt_service.sync_cli --loop --interval-seconds "${SYNC_INTERVAL_SECONDS:-300}" --db-stats
