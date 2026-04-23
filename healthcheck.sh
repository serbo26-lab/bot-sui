#!/usr/bin/env bash
set -e

PROJECT_DIR="/root/bot-sui"
HEALTH_FILE="$PROJECT_DIR/health.json"

if [ ! -f "$HEALTH_FILE" ]; then
  echo "health file not found"
  exit 1
fi

python3 - << 'PY'
import json
from datetime import datetime
from pathlib import Path

health = json.loads(Path("/root/bot-sui/health.json").read_text(encoding="utf-8"))
status = health.get("status", "unknown")
ts = health.get("timestamp")
if status not in {"ok", "starting"}:
    raise SystemExit(f"bad status: {status}")
if ts:
    dt = datetime.fromisoformat(ts)
    age = (datetime.now() - dt).total_seconds()
    if age > 120:
        raise SystemExit(f"stale health file: {age:.0f}s")
print("ok")
PY