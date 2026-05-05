#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/bot-sui"
SERVICE_NAME="bot-sui"
BACKUP_SOURCE="${1:-}"

if [ -z "$BACKUP_SOURCE" ]; then
  echo "Usage:"
  echo "  sudo /opt/bot-sui/restore.sh /opt/bot-sui/backups/YYYYMMDD_HHMMSS"
  echo "  sudo /opt/bot-sui/restore.sh /opt/bot-sui/backups/YYYYMMDD_HHMMSS/database.sqlite"
  exit 1
fi

if [ "$BACKUP_SOURCE" = "/" ]; then
  echo "Invalid backup source: $BACKUP_SOURCE"
  exit 1
fi

if [ -d "$BACKUP_SOURCE" ]; then
  BACKUP_DB="$BACKUP_SOURCE/database.sqlite"
elif [ -f "$BACKUP_SOURCE" ] && [ "$(basename "$BACKUP_SOURCE")" = "database.sqlite" ]; then
  BACKUP_DB="$BACKUP_SOURCE"
else
  echo "Invalid backup source: $BACKUP_SOURCE"
  echo "Expected backup directory or direct path to database.sqlite"
  exit 1
fi

if [ ! -f "$BACKUP_DB" ]; then
  echo "Backup does not contain database.sqlite: $BACKUP_SOURCE"
  exit 1
fi

if [ ! -d "$PROJECT_DIR" ] || [ "$PROJECT_DIR" = "/" ]; then
  echo "Invalid PROJECT_DIR: $PROJECT_DIR"
  exit 1
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
SAFETY_COPY="$PROJECT_DIR/database.sqlite.before_restore_$STAMP"

echo "Stopping $SERVICE_NAME..."
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
fi

if [ -f "$PROJECT_DIR/database.sqlite" ]; then
  cp "$PROJECT_DIR/database.sqlite" "$SAFETY_COPY"
  echo "Current database saved to: $SAFETY_COPY"
fi

cp "$BACKUP_DB" "$PROJECT_DIR/database.sqlite"
chown bot-sui:bot-sui "$PROJECT_DIR/database.sqlite"
chmod 0640 "$PROJECT_DIR/database.sqlite"

echo "Starting $SERVICE_NAME..."
if command -v systemctl >/dev/null 2>&1; then
  systemctl start "$SERVICE_NAME"
  systemctl status "$SERVICE_NAME" --no-pager || true
fi

echo "Restore completed from: $BACKUP_DB"
