#!/usr/bin/env bash
# =============================================================================
# 宿主机/容器环境准备：用 uv 建三个独立虚拟环境
#   .venv-vllm    py>=3.11  推理服务
#   .venv-gateway py>=3.11  OpenAI 兼容网关（纯 CPU）
#   .venv-webui   py>=3.11  Open WebUI（要求 >=3.11）
#
# 关键：不写死 --python 3.12/3.11，而是复用本机已有的 >=3.11 解释器，并加
# --python-preference only-system。否则 uv 会去 GitHub 下 python-build-standalone，
# 在内网/容器里会卡十几分钟最后 CPythonInstallFailed（这是实际踩过的坑）。
# 可重复执行，已存在的 venv 只补装依赖。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs data

UV_BIN="${UV_BIN:-$(command -v uv || echo "${HOME:-/root}/.local/bin/uv")}"
log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install][!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[install][FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

if ! command -v "$UV_BIN" >/dev/null 2>&1 && [ ! -x "$UV_BIN" ]; then
    die "找不到 uv（UV_BIN=$UV_BIN）。安装：python3 -m pip install uv 或 curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# .env 里的 VLLM_VERSION 等就取来用；解析失败不影响（.env 由本脚本末尾生成）
if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -a; . ./.env 2>/dev/null || true; set +a
fi
VLLM_VERSION="${VLLM_VERSION:-0.31.0}"

# ---------------------------------------------------------------- 选解释器
pick_python() {
    local c path
    for c in python3.13 python3.12 python3.11 python3 python; do
        path="$(command -v "$c" 2>/dev/null)" || continue
        [ -n "$path" ] || continue
        if "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            printf '%s' "$path"; return 0
        fi
    done
    return 1
}
PY_BIN="${PY_BIN:-$(pick_python || true)}"
[ -n "$PY_BIN" ] || die "找不到 >=3.11 的解释器（现有 python3: $(python3 -V 2>&1)）。
装一个：conda create -n py311 python=3.11   或   apt install python3.11
也可显式指定：PY_BIN=/path/to/python3.11 bash scripts/install.sh"
log "解释器：$PY_BIN（$("$PY_BIN" -V 2>&1)）"

UV_VENV_ARGS=(--python "$PY_BIN" --python-preference only-system)

# 安装即打印进度，同时留全量日志（原实现 `2>&1 | tail -5` 会把画面冻住两小时，看起来像卡死）
run_pip() {
    local py="$1" label="$2"; shift 2
    local logf="logs/install-${label}.log"
    log "安装 ${label}（完整日志：${logf}）"
    if "$UV_BIN" pip install --python "$py" "$@" 2>&1 | tee -a "$logf"; then
        return 0
    fi
    die "安装 ${label} 失败，详见 ${logf}。常见原因：pypi.org 不通 →
  export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple"
}

need_venv() {  # $1=目录 $2=标签
    local dir="$1" label="$2"
    if [ ! -x "$dir/bin/python" ]; then
        log "创建 $dir（$label）"
        "$UV_BIN" venv "${UV_VENV_ARGS[@]}" "$dir"
    fi
    if ! "$dir/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        die "$dir 里的 Python 版本过低（$("$dir/bin/python" -V 2>&1)）：rm -rf $dir 后重跑本脚本"
    fi
}

# ---------------------------------------------------------------- vLLM
need_venv .venv-vllm "推理服务"
run_pip .venv-vllm/bin/python vllm "vllm==${VLLM_VERSION}"
# flashinfer 采样器首次运行要用 ninja 现场编译 kernel，缺了会 500
run_pip .venv-vllm/bin/python ninja ninja

# ---------------------------------------------------------------- 网关
need_venv .venv-gateway "OpenAI 兼容网关"
run_pip .venv-gateway/bin/python gateway -r requirements-dev.txt

# ---------------------------------------------------------------- Open WebUI
need_venv .venv-webui "Open WebUI"
run_pip .venv-webui/bin/python webui open-webui

# 站点署名（作者：丁亮 ）——写进 Open WebUI 的 static/custom.css
bash ./scripts/patch_webui_branding.sh 2>/dev/null \
    || log "[!] 署名样式安装失败（不影响使用，可稍后执行 bash scripts/patch_webui_branding.sh）"

if [ ! -f .env ]; then
    cp .env.example .env
    log "已生成 .env（记得改 ADMIN_TOKEN / WEBUI_SECRET_KEY）"
fi

# 按本机 GPU 校准 TP/util/max_len：.env.example 的默认值来自 4×4090 那台机器，
# 直接用在单卡上会出现 util×显存 < 权重 的必炸组合
if [ -f scripts/tune_env.sh ]; then
    bash scripts/tune_env.sh || warn "自动调参失败（不影响安装，可稍后 bash scripts/tune_env.sh）"
fi

log "完成。下一步："
log "  1) ./scripts/start_vllm.sh     # 加载 21GB 权重，2~5 分钟"
log "  2) ./scripts/start_gateway.sh"
log "  3) ./scripts/start_webui.sh"
log "  4) ./scripts/smoke_test.sh"
