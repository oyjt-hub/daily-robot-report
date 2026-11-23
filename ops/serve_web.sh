#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIND_IP="${BIND_IP:-127.0.0.1}"
PORT="${PORT:-8765}"

echo "启动本地只读静态日报预览服务: http://$BIND_IP:$PORT/"
exec "$PROJECT_DIR/.venv/bin/python" -m http.server "$PORT" --bind "$BIND_IP" --directory "$PROJECT_DIR"
