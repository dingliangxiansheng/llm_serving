#!/usr/bin/env bash
# =============================================================================
# 一键准备：建虚拟环境（vLLM / 网关 / Open WebUI）+ 下载模型权重
#   · 容器内、宿主机都能跑；不需要 docker，不需要 root
#   · 需要 uv（没有会自动安装：优先 pip 镜像，其次官方脚本）
#   · 会自动探测内网镜像（pypi/github 不通时注入 UV_* 变量）并按本机 GPU 调 .env
#   · 已有环境与权重会跳过，可反复执行
#
# 用法：
#   bash scripts/bootstrap.sh              # 装环境 + 下权重（21GB，中断可重跑续传）
#   bash scripts/bootstrap.sh --start      # 装完直接后台启动整套服务
#   bash scripts/bootstrap.sh --no-weight  # 只装环境，不下载权重
#   bash scripts/bootstrap.sh --local      # 权重强制用本机 venv 下载（容器内推荐）
#   bash scripts/bootstrap.sh --check      # 只体检现状，不做任何修改
#   bash scripts/bootstrap.sh --help
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

START=0
WITH_WEIGHT=1
CHECK_ONLY=0
WEIGHT_LOCAL=0
for arg in "$@"; do
    case "$arg" in
        --start) START=1 ;;
        --no-weight) WITH_WEIGHT=0 ;;
        --local) WEIGHT_LOCAL=1 ;;
        --check) CHECK_ONLY=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "未知参数：$arg（见 --help）" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[1;32m[bootstrap]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap][!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[bootstrap][FATAL]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

UV_BIN="${UV_BIN:-$(command -v uv || echo "${HOME:-/root}/.local/bin/uv")}"
MODEL_PATH_REL="${MODEL_PATH:-./weight/Qwen3.8-27B-AWQ-INT4}"
case "$MODEL_PATH_REL" in /*) WEIGHT_DIR="$MODEL_PATH_REL" ;; *) WEIGHT_DIR="$ROOT/${MODEL_PATH_REL#./}" ;; esac
MIN_BYTES="${WEIGHT_MIN_BYTES:-19000000000}"

weight_bytes() {
    # 权重目录里 *.safetensors 的总字节数
    find "$WEIGHT_DIR" -maxdepth 1 -name '*.safetensors' -type f -printf '%s\n' 2>/dev/null \
        | awk '{s+=$1} END{printf "%.0f", s+0}'
}
weight_gb() { awk -v b="${1:-0}" 'BEGIN{printf "%.2f GB", b/1e9}'; }

# ---------------------------------------------------------------- 体检
if [ "$CHECK_ONLY" = "1" ]; then
    step "环境体检（只读）"
    ok()   { printf '  \033[1;32m[ok]\033[0m %s：%s\n' "$1" "$2"; }
    bad()  { printf '  \033[1;31m[--]\033[0m %s：%s\n' "$1" "$2"; }
    have bash && ok "bash" "$(bash --version | head -1 | awk '{print $4}')" || bad "bash" "缺失"
    have python3 && ok "python3" "$(python3 -V 2>&1 | awk '{print $2}')" || bad "python3" "缺失"
    have "$UV_BIN" && ok "uv" "$("$UV_BIN" --version 2>/dev/null | awk '{print $2}')" \
        || bad "uv" "缺失（python3 -m pip install uv）"
    if have docker; then
        if docker info >/dev/null 2>&1; then
            ok "docker" "守护进程可用（权重走下载器容器）"
        else
            bad "docker" "只有客户端、无守护进程 → 权重走本机 venv 下载（正常，无需处理）"
        fi
    else
        ok "docker" "未安装 → 权重走本机 venv 下载（正常，无需处理）"
    fi
    for pair in ".venv-vllm/bin/vllm:vllm" ".venv-gateway/bin/python:网关" ".venv-webui/bin/open-webui:Open WebUI"; do
        path="${pair%%:*}"; label="${pair##*:}"
        [ -x "$path" ] && ok "$label venv" "$path" || bad "$label venv" "未创建"
    done
    wb="$(weight_bytes)"
    if [ "${wb:-0}" -ge "$MIN_BYTES" ]; then ok "模型权重" "$(weight_gb "$wb")"
    else bad "模型权重" "$(weight_gb "$wb") / 期望 >= $(weight_gb "$MIN_BYTES")"; fi
    if [ -f .env ]; then
        ok ".env" "TP=$(sed -n 's/^TENSOR_PARALLEL_SIZE=//p' .env | tail -1) util=$(sed -n 's/^GPU_MEMORY_UTILIZATION=//p' .env | tail -1) max_len=$(sed -n 's/^MAX_MODEL_LEN=//p' .env | tail -1)"
    else
        bad ".env" "未生成（cp .env.example .env）"
    fi
    if have nvidia-smi; then
        free_mib="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | sort -n | head -1 || echo 0)"
        ok "GPU 空闲（最小卡）" "${free_mib} MiB"
    else
        bad "nvidia-smi" "缺失（没有 GPU 就只能跑网关/WebUI）"
    fi
    avail="$(df -BG --output=avail "$ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')"
    log "磁盘可用（仓库所在分区）：${avail:-?}GB（权重约 21GB）"
    exit 0
fi

# ---------------------------------------------------------------- 前置检查
step "前置检查"
have bash || die "缺少 bash"
have python3 || die "缺少 python3"

# 内网/容器环境：先探测哪些站点不通，再把镜像变量注入本进程（子脚本继承）
# uv 不读 pip.conf / PIP_INDEX_URL，必须显式给 UV_DEFAULT_INDEX
probe() { curl -s -o /dev/null --connect-timeout 5 --max-time 8 "$1"; }
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-60}"
if have curl; then
    if ! probe https://pypi.org/simple/; then
        export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://mirrors.aliyun.com/pypi/simple}"
        warn "pypi.org 不可达 → 本次使用 UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX"
    fi
    if ! probe https://github.com/; then
        export UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/releases/download}"
        warn "github.com 不可达 → 本次使用 UV_PYTHON_INSTALL_MIRROR（gh-proxy 不稳定，优先用系统自带 python3.11+）"
    fi
else
    warn "没有 curl：跳过镜像探测，若装包很慢请手动 export UV_DEFAULT_INDEX"
fi

# uv 是唯一的外部前置依赖：缺了就当场装。
# 通道 1 = pip（pip.conf 已指国内镜像，17MB wheel，秒级）；通道 2 = 官方脚本（必须带超时，
# 否则 releases.astral.sh 慢的时候会「卡住」半小时；原实现还把输出丢进 /dev/null，看不出在干什么）
install_uv() {
    local py found
    for py in python3.13 python3.12 python3.11 python3 python; do
        command -v "$py" >/dev/null 2>&1 || continue
        "$py" -m pip --version >/dev/null 2>&1 || continue
        log "通道 1/2：$py -m pip install uv"
        if "$py" -m pip install -q --upgrade uv >/dev/null 2>&1; then
            found="$(command -v uv 2>/dev/null || true)"
            if [ -n "$found" ]; then UV_BIN="$found"; return 0; fi
        fi
    done
    have curl || return 1
    log "通道 2/2：官方安装脚本（connect 5s / 总计 240s 超时）"
    curl -sSfL --connect-timeout 5 --max-time 240 --retry 1 \
        https://astral.sh/uv/install.sh | sh || return 1
    UV_BIN="${HOME:-/root}/.local/bin/uv"
    [ -x "$UV_BIN" ]
}

if ! have "$UV_BIN" && [ ! -x "$UV_BIN" ]; then
    warn "未找到 uv，自动安装（pip 镜像 → 官方脚本，两条通道依次尝试）"
    install_uv || die "uv 自动安装失败。手动装任选其一：
  python3 -m pip install uv                      # 国内镜像，最快
  curl -LsSf https://astral.sh/uv/install.sh | sh
  也可设置 UV_BIN=/path/to/uv 指定现成的 uv"
    log "uv 已安装：$UV_BIN"
fi
log "uv: $("$UV_BIN" --version 2>/dev/null || echo "$UV_BIN")"

if have nvidia-smi; then
    log "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
    warn "没有 nvidia-smi：本机只能跑网关与 WebUI，vLLM 起不来"
fi

# 容器里常见「装了 docker 客户端但没有守护进程」：明确告知，别让人以为要去宿主机绕一圈
if have docker && ! docker info >/dev/null 2>&1; then
    warn "docker 客户端存在但守护进程不可用 → 容器内模式：权重用本机 modelscope 下载"
fi

avail_gb="$(df -BG --output=avail "$ROOT" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)"
if [ "${avail_gb:-0}" -lt 30 ] && [ "$WITH_WEIGHT" = "1" ]; then
    warn "仓库所在分区仅剩 ${avail_gb}GB，权重需要约 21GB，可能装不下"
fi
# uv 缓存默认在 ~/.cache/uv（和仓库同分区时会被这里漏掉），装完 vLLM 大约占 15GB
uv_cache_dir="${UV_CACHE_DIR:-${HOME:-/root}/.cache/uv}"
uv_parent_avail="$(df -BG --output=avail "$(dirname "$uv_cache_dir")" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)"
if [ "${uv_parent_avail:-0}" -lt 20 ]; then
    warn "uv 缓存分区（$(dirname "$uv_cache_dir")）只剩 ${uv_parent_avail}GB：装 vLLM 需要约 15GB 暂存，装完可 uv cache prune 回收"
fi

mkdir -p logs data

# ---------------------------------------------------------------- 1/3 虚拟环境
step "1/3 创建虚拟环境并安装依赖（vLLM 约 2~3GB、WebUI 约 1GB，首次较慢）"
bash scripts/install.sh

# ---------------------------------------------------------------- 2/3 权重
if [ "$WITH_WEIGHT" = "1" ]; then
    step "2/3 下载模型权重（已完整会自动跳过；中断可重跑续传）"
    if [ "$WEIGHT_LOCAL" = "1" ]; then
        bash scripts/fetch_weight.sh --local
    else
        bash scripts/fetch_weight.sh
    fi
else
    step "2/3 跳过权重下载（--no-weight）"
fi

# ---------------------------------------------------------------- 3/3 结论
step "3/3 完成"
wb="$(weight_bytes)"
log "仓库：  $ROOT"
log "权重：  $WEIGHT_DIR（$(weight_gb "$wb")）"
log "配置：  .env（TP/util/max_len 已按本机 GPU 自动校准）"

if [ "$START" = "1" ]; then
    step "后台启动整套服务（vLLM 首次加载约 2~5 分钟）"
    bash scripts/start_all.sh -d
    port="$(sed -n 's/^WEBUI_PORT=\([0-9]*\).*/\1/p' .env 2>/dev/null | tail -1)"
    gport="$(sed -n 's/^GATEWAY_PORT=\([0-9]*\).*/\1/p' .env 2>/dev/null | tail -1)"
    log "已启动。日志：logs/vllm.log / logs/gateway.log / logs/webui.log"
    log "打开：  http://127.0.0.1:${port:-8080}   （网关 API：http://127.0.0.1:${gport:-9000}/v1）"
    log "查看进度：bash scripts/smoke_test.sh   （或 tail -f logs/vllm.log）"
else
    log "下一步：bash scripts/start_all.sh          # 一键启动（首次约 2~5 分钟）"
    log "       bash scripts/smoke_test.sh         # 启动后端到端自检"
fi
