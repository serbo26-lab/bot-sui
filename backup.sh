#!/usr/bin/env bash
set -e

PROJECT_DIR="/root/bot-sui"
BACKUP_DIR="$PROJECT_DIR/backups"
STAMP="$(date +%Y%m%d_%H%M%S)"
SNAPSHOT_DIR="$BACKUP_DIR/$STAMP"

mkdir -p "$SNAPSHOT_DIR"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required for safe backups. Install it: apt install sqlite3"
  exit 1
fi

for file in payments.db support.db reminders.db antiabuse.db tariffs.db trials.db tgproxy.db; do
  if [ -f "$PROJECT_DIR/$file" ]; then
    sqlite3 "$PROJECT_DIR/$file" ".backup '$SNAPSHOT_DIR/$file'"
  fi
done

if [ -f "$PROJECT_DIR/health.json" ]; then
  cp "$PROJECT_DIR/health.json" "$SNAPSHOT_DIR/health.json"
fi

if [ -d "$PROJECT_DIR/logs" ]; then
  tar -czf "$SNAPSHOT_DIR/logs.tar.gz" -C "$PROJECT_DIR" logs
fi

echo "Backup created in $SNAPSHOT_DIR"