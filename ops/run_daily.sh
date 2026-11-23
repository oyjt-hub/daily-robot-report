#!/usr/bin/env bash
set -euo pipefail

export TZ="Asia/Shanghai"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/state"

exec 9>"$PROJECT_DIR/state/run.lock"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 检测到另一个 daily-robot-report 实例正在运行，本次跳过。"
  exit 0
fi

if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

USE_MOCK=0
for arg in "$@"; do
  if [ "$arg" = "--mock-llm" ]; then
    USE_MOCK=1
    break
  fi
done

is_unset_key() {
  local v="${1:-}"
  [ -z "$v" ] || [[ "$v" == YOUR_* ]]
}

if [ "$USE_MOCK" -eq 0 ]; then
  if is_unset_key "${LLM_API_KEY:-}" && is_unset_key "${GEMINI_API_KEY:-}" && is_unset_key "${DASHSCOPE_API_KEY:-}"; then
    echo "[$(date '+%F %T')] [预检提示] 尚未配置真实 GEMINI_API_KEY / LLM_API_KEY / DASHSCOPE_API_KEY。请先配置 .env 后运行，本次安全退出。"
    exit 0
  fi
fi

LOG_FILE="$PROJECT_DIR/logs/daily-$(date +%F).log"
echo "=== [$(date '+%F %T')] 开始运行 daily-robot-report ===" >> "$LOG_FILE"
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/main.py" "$@" 2>&1 | tee -a "$LOG_FILE"

find "$PROJECT_DIR/logs" -name 'daily-*.log' -type f -mtime +30 -delete 2>/dev/null || true
