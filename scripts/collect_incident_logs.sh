#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/bot-sui"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="$PROJECT_DIR/incident_logs_$STAMP"

mkdir -p "$OUT_DIR"

if command -v journalctl >/dev/null 2>&1; then
  journalctl -u bot-sui --since "24 hours ago" --no-pager > "$OUT_DIR/journal_bot_sui_24h.log" || true
fi

if [ -d "$PROJECT_DIR/logs" ]; then
  cp -a "$PROJECT_DIR/logs" "$OUT_DIR/logs" || true
fi

if [ -f "$PROJECT_DIR/health.json" ]; then
  cp "$PROJECT_DIR/health.json" "$OUT_DIR/health.json" || true
fi

tar -czf "$OUT_DIR.tar.gz" -C "$PROJECT_DIR" "$(basename "$OUT_DIR")"
rm -rf "$OUT_DIR"

echo "Incident logs collected: $OUT_DIR.tar.gz"
