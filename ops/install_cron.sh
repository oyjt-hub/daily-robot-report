#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="# DAILY-ROBOT-REPORT-MANAGED"
CRON_LINE="0 10 * * 1-5 bash $PROJECT_DIR/ops/run_daily.sh >> $PROJECT_DIR/logs/cron.log 2>&1 $TAG"

EXISTING="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf '%s\n' "$EXISTING" | grep -vF "$TAG" || true)"

{
  if [ -n "$FILTERED" ]; then
    printf '%s\n' "$FILTERED"
  fi
  printf '%s\n' "$CRON_LINE"
} | crontab -

echo "已安装 cron 任务：$CRON_LINE"
