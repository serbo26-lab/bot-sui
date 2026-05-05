#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/bot-sui"
BACKUP_DIR="$PROJECT_DIR/backups"
STAMP="$(date +%Y%m%d_%H%M%S)"
SNAPSHOT_DIR="$BACKUP_DIR/$STAMP"
RETENTION_DAYS="${BOT_SUI_BACKUP_RETENTION_DAYS:-7}"
KEEP_LAST="${BOT_SUI_BACKUP_KEEP_LAST:-7}"

mkdir -p "$SNAPSHOT_DIR"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required for safe backups. Install it: apt install sqlite3"
  exit 1
fi

if [ -f "$PROJECT_DIR/database.sqlite" ]; then
  sqlite3 "$PROJECT_DIR/database.sqlite" ".backup '$SNAPSHOT_DIR/database.sqlite'"
fi

for f in config.json config.example.json bot.py requirements.txt health.json nodes.json *.sh OPERATIONS.md; do
  if [ -f "$PROJECT_DIR/$f" ]; then
    cp -a "$PROJECT_DIR/$f" "$SNAPSHOT_DIR/" 2>/dev/null || true
  fi
done

for d in keys certs systemd; do
  if [ -d "$PROJECT_DIR/$d" ]; then
    mkdir -p "$SNAPSHOT_DIR/$d"
    rsync -a --delete "$PROJECT_DIR/$d/" "$SNAPSHOT_DIR/$d/"
  fi
done

# Daily backup is an operational state snapshot, not an archive of history.
# Never include old backups/logs/releases/migration packages into itself.
find "$SNAPSHOT_DIR" -type f \( -name '*.tar.gz' -o -name '*.zip' -o -name '*.tgz' -o -name '*.7z' \) -delete 2>/dev/null || true
date +%s > "$PROJECT_DIR/.last_backup" 2>/dev/null || true

# Keep lightweight daily snapshots only. Full migration/root packages are created separately.
# Default retention: last 7 days and at least last 7 snapshots.
if [ -z "${BACKUP_DIR:-}" ] || [ "$BACKUP_DIR" = "/" ]; then
  echo "Critical error: BACKUP_DIR is empty or root; refusing cleanup"
  exit 1
fi

if [ -d "$BACKUP_DIR" ]; then
  find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -exec rm -rf {} +
  # If manual runs created too many snapshots inside the retention window, keep the newest KEEP_LAST directories.
  mapfile -t OLD_BACKUPS < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p
' | sort -nr | awk -v keep="$KEEP_LAST" 'NR>keep {print $2}')
  for old in "${OLD_BACKUPS[@]:-}"; do
    [ -n "$old" ] && rm -rf "$old"
  done
fi

if id bot-sui >/dev/null 2>&1; then
  chown -R bot-sui:bot-sui "$BACKUP_DIR"
fi

SIZE="$(du -sh "$SNAPSHOT_DIR" 2>/dev/null | awk '{print $1}')"
echo "Backup created in $SNAPSHOT_DIR (${SIZE:-unknown})"
echo "Retention: ${RETENTION_DAYS} days, keep last ${KEEP_LAST} snapshots"
