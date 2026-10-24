#!/usr/bin/env bash
# =============================================================================
# 启动 Open WebUI，指向本仓库网关
#
# 关键点：ENABLE_FORWARD_USER_INFO_HEADERS=true
#   Open WebUI 只有在开启它时才会把 X-OpenWebUI-User-Id / -Chat-Id 传给后端，
#   网关靠这两个头区分「谁」和「哪个会话」。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a; source ./.env; set +a
fi

export OPENAI_API_BASE_URL="${OPENAI_API_BASE_URL:-${UPSTREAM_BASE_URL:-http://127.0.0.1:8000/v1}}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-dummy}"
export OPENAI_API_BASE_URLS="$OPENAI_API_BASE_URL"
export OPENAI_API_KEYS="$OPENAI_API_KEY"
export ENABLE_OLLAMA_API="${ENABLE_OLLAMA_API:-false}"
export ENABLE_FORWARD_USER_INFO_HEADERS=true
export WEBUI_AUTH="${WEBUI_AUTH:-true}"
export WEBUI_SECRET_KEY="${WEBUI_SECRET_KEY:-$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-32)}"
export WEBUI_URL="${WEBUI_URL:-http://0.0.0.0:${WEBUI_PORT:-8080}}"
export DATA_DIR="${DATA_DIR:-$ROOT/data/webui}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export ENABLE_VERSION_CHECK="${ENABLE_VERSION_CHECK:-false}"

mkdir -p "$DATA_DIR"

BIN="$ROOT/.venv-webui/bin/open-webui"
[[ -x "$BIN" ]] || BIN="$(command -v open-webui || true)"
[[ -n "$BIN" ]] || { echo "[x] 找不到 open-webui" >&2; exit 1; }

# 站点署名：把 webui/branding/custom.css 装进 Open WebUI 的 static/custom.css
# （幂等；装不上只告警，不影响服务启动）
bash "$ROOT/scripts/patch_webui_branding.sh" >/dev/null 2>&1 \
  || echo "[!] 署名样式安装失败，页面将不显示署名（不影响使用）" >&2

exec "$BIN" serve --host 0.0.0.0 --port "${WEBUI_PORT:-8080}"
