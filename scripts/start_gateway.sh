#!/usr/bin/env bash
# =============================================================================
# 启动网关（用户隔离 + 历史 + 长期记忆 + 限流）
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a; source ./.env; set +a
fi

PY="$ROOT/.venv-gateway/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

export QIANWEN_SERVER__PORT="${QIANWEN_SERVER__PORT:-${GATEWAY_PORT:-9000}}"
export QIANWEN_UPSTREAM__BASE_URL="${QIANWEN_UPSTREAM__BASE_URL:-${UPSTREAM_BASE_URL:-http://127.0.0.1:8000/v1}}"
export QIANWEN_AUTH__MODE="${QIANWEN_AUTH__MODE:-${AUTH_MODE:-header}}"
export QIANWEN_LIMITS__MAX_CONCURRENT="${QIANWEN_LIMITS__MAX_CONCURRENT:-${GATEWAY_MAX_CONCURRENT:-4}}"

if [[ -n "${ADMIN_TOKEN:-}" && -z "${QIANWEN_AUTH__ADMIN_TOKEN:-}" ]]; then
  export QIANWEN_AUTH__ADMIN_TOKEN="$ADMIN_TOKEN"
fi
if [[ -n "${GATEWAY_DB_PATH:-}" && -z "${QIANWEN_STORAGE__DB_PATH:-}" ]]; then
  export QIANWEN_STORAGE__DB_PATH="$GATEWAY_DB_PATH"
fi

exec "$PY" -m gateway
