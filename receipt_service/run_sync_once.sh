#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCK_DIR="$SCRIPT_DIR/.sync_once.lock"

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  PYTHON_BIN="python3"
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "{\"time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"status\":\"skip\",\"message\":\"sync is already running\"}"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$PROJECT_DIR"
"$PYTHON_BIN" -m receipt_service.sync_cli --db-stats
