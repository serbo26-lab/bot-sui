#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/bot-sui"
DB="$PROJECT_DIR/database.sqlite"
BACKUP_DIR="$PROJECT_DIR/backups"
LOG_DIR="$PROJECT_DIR/logs"
INCIDENT_DIR="$PROJECT_DIR/incident_logs"
RELEASE_DIR="$PROJECT_DIR/releases"

# Running this script as `sudo -u bot-sui /opt/bot-sui/maintenance.sh` from /root
# can break GNU find when it tries to restore an unreadable initial cwd.
# Always move into the project directory first.
cd "$PROJECT_DIR"

BACKUP_RETENTION_DAYS="${BOT_SUI_BACKUP_RETENTION_DAYS:-7}"
BACKUP_KEEP_LAST="${BOT_SUI_BACKUP_KEEP_LAST:-7}"
INCIDENT_RETENTION_DAYS="${BOT_SUI_INCIDENT_RETENTION_DAYS:-30}"
RELEASE_KEEP_LAST="${BOT_SUI_RELEASE_KEEP_LAST:-5}"
LOG_RETENTION_DAYS="${BOT_SUI_LOG_RETENTION_DAYS:-14}"
ANTIABUSE_RAW_RETENTION_DAYS="${BOT_SUI_ANTIABUSE_RAW_RETENTION_DAYS:-7}"
ANTIABUSE_EVENT_RETENTION_DAYS="${BOT_SUI_ANTIABUSE_EVENT_RETENTION_DAYS:-30}"
PAYMENT_EVENT_RETENTION_DAYS="${BOT_SUI_PAYMENT_EVENT_RETENTION_DAYS:-90}"
WATCHDOG_EVENT_RETENTION_DAYS="${BOT_SUI_WATCHDOG_EVENT_RETENTION_DAYS:-365}"
VACUUM_MIN_FREE_MB="${BOT_SUI_VACUUM_MIN_FREE_MB:-512}"
VACUUM_PERIOD_DAYS="${BOT_SUI_VACUUM_PERIOD_DAYS:-7}"
ANTIABUSE_RAW_MAX_ROWS="${BOT_SUI_ANTIABUSE_RAW_MAX_ROWS:-150000}"
ANTIABUSE_MATCHES_MAX_ROWS="${BOT_SUI_ANTIABUSE_MATCHES_MAX_ROWS:-50000}"
ANTIABUSE_ALERTS_MAX_ROWS="${BOT_SUI_ANTIABUSE_ALERTS_MAX_ROWS:-20000}"
ANTIABUSE_WARNINGS_MAX_ROWS="${BOT_SUI_ANTIABUSE_WARNINGS_MAX_ROWS:-20000}"
ANTIABUSE_REMOTE_EVENTS_MAX_ROWS="${BOT_SUI_ANTIABUSE_REMOTE_EVENTS_MAX_ROWS:-50000}"

safe_rm_dirs_by_age_and_count() {
  local dir="$1" days="$2" keep="$3"
  [ -d "$dir" ] || return 0
  case "$dir" in
    "$PROJECT_DIR"/*) ;;
    *) echo "Refusing cleanup outside project dir: $dir" >&2; return 1 ;;
  esac
  find "$dir" -mindepth 1 -maxdepth 1 -type d -mtime +"$days" -exec rm -rf {} +
  mapfile -t old_items < <(find "$dir" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p
' 2>/dev/null | sort -nr | awk -v keep="$keep" 'NR>keep {print $2}')
  for old in "${old_items[@]:-}"; do
    [ -n "$old" ] && rm -rf "$old"
  done
}

safe_rm_files_by_age() {
  local dir="$1" days="$2"
  shift 2
  [ -d "$dir" ] || return 0
  case "$dir" in
    "$PROJECT_DIR"/*) ;;
    *) echo "Refusing file cleanup outside project dir: $dir" >&2; return 1 ;;
  esac
  find "$dir" -type f \( "$@" \) -mtime +"$days" -delete 2>/dev/null || true
}

# 1) Lightweight backup retention. Root migration packages are separate and not kept here.
safe_rm_dirs_by_age_and_count "$BACKUP_DIR" "$BACKUP_RETENTION_DAYS" "$BACKUP_KEEP_LAST"

# 2) Debug/incident/release retention.
safe_rm_dirs_by_age_and_count "$INCIDENT_DIR" "$INCIDENT_RETENTION_DAYS" 20
safe_rm_dirs_by_age_and_count "$RELEASE_DIR" 365 "$RELEASE_KEEP_LAST"

# 3) Local archived logs and accidental archives in the project tree.
safe_rm_files_by_age "$LOG_DIR" "$LOG_RETENTION_DAYS" -name '*.log.*' -o -name '*.gz' -o -name '*.zip'
safe_rm_files_by_age "$PROJECT_DIR" 3 -name 'bot-sui-root-migration-*.tar.gz' -o -name 'bot-sui-migration-*.tar.gz' -o -name 'bot-sui-backup-*.tar.gz' -o -name '*.zip' -o -name '*.tgz' -o -name '*.7z'

# 4) SQLite hygiene: keep operational history bounded and checkpoint WAL.
if [ -f "$DB" ]; then
  python3 - <<'PYSQLCLEAN'
import os, sqlite3, time
from pathlib import Path

db = Path(os.environ.get('BOT_SUI_DB_FOR_MAINTENANCE', '/opt/bot-sui/database.sqlite'))
now = time.time()
raw_cutoff = now - int(os.environ.get('BOT_SUI_ANTIABUSE_RAW_RETENTION_DAYS', '7')) * 86400
event_cutoff = now - int(os.environ.get('BOT_SUI_ANTIABUSE_EVENT_RETENTION_DAYS', '30')) * 86400
payment_cutoff = now - int(os.environ.get('BOT_SUI_PAYMENT_EVENT_RETENTION_DAYS', '90')) * 86400
watchdog_cutoff = now - int(os.environ.get('BOT_SUI_WATCHDOG_EVENT_RETENTION_DAYS', '365')) * 86400
raw_max_rows = int(os.environ.get('BOT_SUI_ANTIABUSE_RAW_MAX_ROWS', '150000'))
matches_max_rows = int(os.environ.get('BOT_SUI_ANTIABUSE_MATCHES_MAX_ROWS', '50000'))
# Doctor uses *_MAX_ROWS as the red line. Maintenance trims with headroom so
# active workers do not immediately cross the cap again after cleanup.
raw_target_rows = int(os.environ.get('BOT_SUI_ANTIABUSE_RAW_TARGET_ROWS', '140000'))
matches_target_rows = int(os.environ.get('BOT_SUI_ANTIABUSE_MATCHES_TARGET_ROWS', '45000'))
alerts_max_rows = int(os.environ.get('BOT_SUI_ANTIABUSE_ALERTS_MAX_ROWS', '20000'))
warnings_max_rows = int(os.environ.get('BOT_SUI_ANTIABUSE_WARNINGS_MAX_ROWS', '20000'))
remote_events_max_rows = int(os.environ.get('BOT_SUI_ANTIABUSE_REMOTE_EVENTS_MAX_ROWS', '50000'))
raw_target_rows = min(raw_target_rows, raw_max_rows) if raw_max_rows > 0 else raw_target_rows
matches_target_rows = min(matches_target_rows, matches_max_rows) if matches_max_rows > 0 else matches_target_rows

conn = sqlite3.connect(str(db), timeout=30)
try:
    conn.execute("PRAGMA busy_timeout=30000")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    deletes = []
    def delete_if(table, where, params=()):
        if table in tables:
            cur = conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            deletes.append((table, cur.rowcount if cur.rowcount is not None else 0))

    def cap_table(table, max_rows, order_col):
        if table not in tables or max_rows <= 0:
            return
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return
        excess = int(total or 0) - int(max_rows)
        if excess <= 0:
            return
        # Delete oldest rows by rowid selected through a deterministic ordered subquery.
        cur = conn.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY {order_col} ASC, rowid ASC LIMIT ?)",
            (excess,),
        )
        deletes.append((f"{table}_hard_cap", cur.rowcount if cur.rowcount is not None else 0))

    delete_if('raw_events', 'ts_epoch < ?', (raw_cutoff,))
    delete_if('matches', 'ts_epoch < ?', (raw_cutoff,))
    delete_if('ip_state', 'last_seen < ?', (event_cutoff,))
    delete_if('alerts', 'sent_at < ?', (event_cutoff,))
    delete_if('antiabuse_notifications', 'sent_at < ?', (event_cutoff,))
    delete_if('antiabuse_warnings', 'created_at < ?', (event_cutoff,))
    delete_if('antiabuse_events', 'last_seen < ?', (event_cutoff,))
    delete_if('remote_antiabuse_events', 'last_seen < ?', (event_cutoff,))
    delete_if('remote_antiabuse_notifications', 'sent_at < ?', (event_cutoff,))
    delete_if('payment_status_events', 'created_at < ?', (payment_cutoff,))
    delete_if('watchdog_events', 'created_at < ?', (watchdog_cutoff,))

    # Hard row caps protect SQLite from sudden log bursts even inside the time-retention window.
    # raw_events/matches are trimmed below the doctor cap to leave headroom for live workers.
    cap_table('raw_events', raw_target_rows, 'ts_epoch')
    cap_table('matches', matches_target_rows, 'ts_epoch')
    cap_table('alerts', alerts_max_rows, 'sent_at')
    cap_table('antiabuse_warnings', warnings_max_rows, 'created_at')
    cap_table('remote_antiabuse_events', remote_events_max_rows, 'last_seen')
    conn.commit()
    conn.execute('PRAGMA optimize')
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.commit()
    for table, count in deletes:
        if count:
            print(f"SQLite cleanup: {table} deleted {count}")
finally:
    conn.close()
PYSQLCLEAN
fi

# 4b) Defensive rowid hard caps. This is intentionally independent from
# timestamp columns and always keeps the newest rows by rowid. It protects
# older/migrated schemas where ts_epoch/sent_at based caps may not fire.
if [ -f "$DB" ]; then
  python3 - <<'PYSQLCAP'
import sqlite3
from pathlib import Path

db = Path('/opt/bot-sui/database.sqlite')
limits = {
    'raw_events': 150000,
    'matches': 50000,
    'alerts': 20000,
    'antiabuse_warnings': 20000,
    'remote_antiabuse_events': 50000,
}
conn = sqlite3.connect(str(db), timeout=30)
try:
    conn.execute('PRAGMA busy_timeout=30000')
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, limit in limits.items():
        if table not in tables:
            continue
        try:
            total = int(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] or 0)
        except Exception as exc:
            print(f'SQLite hard cap skipped {table}: {exc}')
            continue
        excess = total - int(limit)
        if excess <= 0:
            continue
        cur = conn.execute(
            f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY rowid ASC LIMIT ?)',
            (excess,),
        )
        print(f'SQLite hard cap: {table} deleted {cur.rowcount if cur.rowcount is not None else excess}')
    conn.commit()
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.commit()
finally:
    conn.close()
PYSQLCAP
fi

# 5) Vacuum is intentionally not daily. It can briefly lock SQLite and is only
# run weekly by default or when BOT_SUI_FORCE_VACUUM=1 is set.
if [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  FREE_MB="$(df -Pm "$PROJECT_DIR" | awk 'NR==2 {print $4}')"
  LAST_VACUUM_FILE="$PROJECT_DIR/.last_vacuum"
  NOW_EPOCH="$(date +%s)"
  LAST_VACUUM="0"
  [ -f "$LAST_VACUUM_FILE" ] && LAST_VACUUM="$(cat "$LAST_VACUUM_FILE" 2>/dev/null || echo 0)"
  AGE_DAYS=$(( (NOW_EPOCH - ${LAST_VACUUM:-0}) / 86400 ))
  if [ "${BOT_SUI_FORCE_VACUUM:-0}" = "1" ] || { [ "$AGE_DAYS" -ge "$VACUUM_PERIOD_DAYS" ] && [ -n "${FREE_MB:-}" ] && [ "$FREE_MB" -ge "$VACUUM_MIN_FREE_MB" ]; }; then
    sqlite3 "$DB" "VACUUM;" && date +%s > "$LAST_VACUUM_FILE" || true
  else
    echo "Skipping VACUUM: age=${AGE_DAYS}d period=${VACUUM_PERIOD_DAYS}d free=${FREE_MB:-unknown}MB min=${VACUUM_MIN_FREE_MB}MB"
  fi
fi

if id bot-sui >/dev/null 2>&1; then
  chown -R bot-sui:bot-sui "$PROJECT_DIR"/backups "$PROJECT_DIR"/logs "$PROJECT_DIR"/incident_logs 2>/dev/null || true
fi

date +%s > "$PROJECT_DIR/.last_maintenance" 2>/dev/null || true

echo "Maintenance completed"
echo "Retention: backups=${BACKUP_RETENTION_DAYS}d/${BACKUP_KEEP_LAST} last, logs=${LOG_RETENTION_DAYS}d, incidents=${INCIDENT_RETENTION_DAYS}d, raw antiabuse=${ANTIABUSE_RAW_RETENTION_DAYS}d, vacuum=${VACUUM_PERIOD_DAYS}d"
echo "Hard caps: raw_events=${ANTIABUSE_RAW_MAX_ROWS}, matches=${ANTIABUSE_MATCHES_MAX_ROWS}, alerts=${ANTIABUSE_ALERTS_MAX_ROWS}, warnings=${ANTIABUSE_WARNINGS_MAX_ROWS}, remote_events=${ANTIABUSE_REMOTE_EVENTS_MAX_ROWS}"
echo "Maintenance targets: raw_events=${BOT_SUI_ANTIABUSE_RAW_TARGET_ROWS:-140000}, matches=${BOT_SUI_ANTIABUSE_MATCHES_TARGET_ROWS:-45000}"
