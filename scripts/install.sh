#!/usr/bin/env bash
# =============================================================================
# 宿主机环境准备：用 uv 建三个独立虚拟环境（系统 python3.10 没有 ensurepip，不能 python -m venv）
#   .venv-vllm    py3.12  推理服务
#   .venv-gateway py3.12  OpenAI 兼容网关（纯 CPU）
#   .venv-webui   py3.11  Open WebUI（要求 >=3.11）
# 可重复执行，已存在的 venv 只补装依赖。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UV_BIN="${UV_BIN:-$(command -v uv || echo "${HOME:-/root}/.local/bin/uv")}"
log() { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[install][FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$UV_BIN" ] || die "找不到 uv。安装：curl -LsSf https://astral.sh/uv/install.sh | sh"

VLLM_VERSION="${VLLM_VERSION:-0.31.0}"

# ---------------------------------------------------------------- vLLM
if [ ! -x ".venv-vllm/bin/python" ]; then
    log "创建 .venv-vllm（CPython 3.12）"
    "$UV_BIN" venv --python 3.12 .venv-vllm
fi
log "安装 vllm==${VLLM_VERSION}（约 2~3GB，耐心等）"
"$UV_BIN" pip install --python .venv-vllm/bin/python "vllm==${VLLM_VERSION}" 2>&1 | tail -5
# flashinfer 采样器首次运行要用 ninja 现场编译 kernel，缺了会 500
"$UV_BIN" pip install --python .venv-vllm/bin/python ninja 2>&1 | tail -2

# ---------------------------------------------------------------- 网关
if [ ! -x ".venv-gateway/bin/python" ]; then
    log "创建 .venv-gateway（CPython 3.12）"
    "$UV_BIN" venv --python 3.12 .venv-gateway
fi
log "安装网关依赖（含 pytest，便于跑单测）"
"$UV_BIN" pip install --python .venv-gateway/bin/python -r requirements-dev.txt 2>&1 | tail -3

# ---------------------------------------------------------------- Open WebUI
if [ ! -x ".venv-webui/bin/python" ]; then
    log "创建 .venv-webui（CPython 3.11，Open WebUI 要求 >=3.11）"
    "$UV_BIN" venv --python 3.11 .venv-webui
fi
log "安装 open-webui（较大，约 1GB）"
"$UV_BIN" pip install --python .venv-webui/bin/python open-webui 2>&1 | tail -3

# 站点署名（作者：丁亮 ）——写进 Open WebUI 的 static/custom.css
bash ./scripts/patch_webui_branding.sh 2>/dev/null \
    || log "[!] 署名样式安装失败（不影响使用，可稍后执行 bash scripts/patch_webui_branding.sh）"

mkdir -p logs data
if [ ! -f .env ]; then
    cp .env.example .env
    log "已生成 .env（记得改 ADMIN_TOKEN / WEBUI_SECRET_KEY）"
fi

log "完成。下一步："
log "  1) ./scripts/start_vllm.sh     # 加载 21GB 权重，2~5 分钟"
log "  2) ./scripts/start_gateway.sh"
log "  3) ./scripts/start_webui.sh"
log "  4) ./scripts/smoke_test.sh"
