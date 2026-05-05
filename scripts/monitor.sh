#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/bot-sui"
HEALTH_FILE="$PROJECT_DIR/health.json"
BACKUP_DIR="$PROJECT_DIR/backups"
MIN_FREE_MB=512
MAX_HEALTH_AGE_SEC=180
MAX_BACKUP_AGE_SEC=90000

fail() {
  echo "MONITOR_FAIL: $*"
  exit 1
}

[ -d "$PROJECT_DIR" ] || fail "project dir missing: $PROJECT_DIR"
[ -f "$HEALTH_FILE" ] || fail "health file missing: $HEALTH_FILE"

python3 - << PY
import json
from datetime import datetime
from pathlib import Path
health_path = Path("$HEALTH_FILE")
data = json.loads(health_path.read_text(encoding="utf-8"))
status = data.get("status")
ts = data.get("timestamp")
if status not in {"ok", "starting"}:
    raise SystemExit(f"bad health status: {status}")
if not ts:
    raise SystemExit("health timestamp missing")
dt = datetime.fromisoformat(ts)
age = (datetime.now() - dt).total_seconds()
if age > $MAX_HEALTH_AGE_SEC:
    raise SystemExit(f"stale health: {age:.0f}s")
print(f"health ok: {status}, age={age:.0f}s")
PY

FREE_MB="$(df -Pm "$PROJECT_DIR" | awk 'NR==2 {print $4}')"
if [ -z "$FREE_MB" ] || [ "$FREE_MB" -lt "$MIN_FREE_MB" ]; then
  fail "low disk space: ${FREE_MB:-unknown} MB free"
fi

if [ -d "$BACKUP_DIR" ]; then
  LAST_BACKUP="$(find "$BACKUP_DIR" -mindepth 2 -maxdepth 2 -name database.sqlite -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | awk '{print $1}')"
  if [ -n "$LAST_BACKUP" ]; then
    NOW="$(date +%s)"
    BACKUP_AGE="$(awk -v now="$NOW" -v last="$LAST_BACKUP" 'BEGIN {print int(now-last)}')"
    if [ "$BACKUP_AGE" -gt "$MAX_BACKUP_AGE_SEC" ]; then
      fail "last backup is too old: ${BACKUP_AGE}s"
    fi
    echo "backup ok: age=${BACKUP_AGE}s"
  else
    echo "backup warning: no database backup found yet"
  fi
else
  echo "backup warning: backup dir missing yet"
fi

echo "monitor ok: free=${FREE_MB}MB"
