#!/usr/bin/env bash
# =============================================================================
# 下载模型权重到宿主机 ./weight/<模型名>（用 modelscope，走「下载器容器」）
#
# 为什么用容器下：docker build 无法往宿主机写文件，所以把下载器做成镜像，
# 运行时 bind-mount 宿主机目录，结果直接落在宿主机上，且能跟着 compose 一起编排。
# 已有完整权重时会自动跳过；中断后重跑会逐文件续传。
#
# 用法：
#   bash scripts/fetch_weight.sh              # 下载（已完整则跳过）+ 构建下载器镜像
#   bash scripts/fetch_weight.sh --check      # 只校验现有权重完整性，不下载
#   bash scripts/fetch_weight.sh --force      # 忽略已存在文件，强制重新下载
#   bash scripts/fetch_weight.sh --no-build   # 不重建下载器镜像（镜像已存在时更快）
#
# 依赖的环境变量（.env 里可覆盖，都有默认值）：
#   MODEL_ID / MODEL_REVISION / WEIGHT_NAME / MODEL_PATH
#   MODELSCOPE_VERSION / DOWNLOAD_WORKERS / WEIGHT_MIN_BYTES / IMAGE_TAG
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FORCE=0
CHECK_ONLY=0
NO_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --check) CHECK_ONLY=1 ;;
        --no-build) NO_BUILD=1 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\033[1;32m[fetch_weight]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[fetch_weight]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[fetch_weight][FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

# 先记住调用者显式传入的变量：.env 是默认值，不该覆盖命令行/上级脚本传入的显式配置
_SAVED_ENV=()
for _var in MODEL_ID MODEL_REVISION WEIGHT_NAME MODEL_PATH MODELSCOPE_VERSION \
            DOWNLOAD_WORKERS WEIGHT_MIN_BYTES IMAGE_TAG; do
    if [ -n "${!_var:-}" ]; then _SAVED_ENV+=("${_var}=${!_var}"); fi
done

if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -a; . ./.env; set +a
fi

for _kv in ${_SAVED_ENV[@]+"${_SAVED_ENV[@]}"}; do
    export "${_kv?}"
done

MODEL_ID="${MODEL_ID:-cyankiwi/Qwen3.8-27B-AWQ-INT4}"
MODEL_REVISION="${MODEL_REVISION:-master}"
WEIGHT_NAME="${WEIGHT_NAME:-$(basename "$MODEL_ID")}"
MODELSCOPE_VERSION="${MODELSCOPE_VERSION:-1.40.2}"
DOWNLOAD_WORKERS="${DOWNLOAD_WORKERS:-8}"
MIN_BYTES="${WEIGHT_MIN_BYTES:-19000000000}"
IMAGE="llm-serving-weights:${IMAGE_TAG:-1.0.0}"

# ------------------------------------------------------------------ 目标目录
# 优先用 .env 的 MODEL_PATH（compose 挂载的就是它），保证「下载位置 == 部署读的位置」
TARGET="${MODEL_PATH:-weight/${WEIGHT_NAME}}"
case "$TARGET" in
    /*) ;;
    *) TARGET="$ROOT_DIR/${TARGET#./}" ;;
esac
TARGET="${TARGET%/}"
WEIGHT_PARENT="$(dirname "$TARGET")"
WEIGHT_BASENAME="$(basename "$TARGET")"
mkdir -p "$WEIGHT_PARENT"

log "模型:     ${MODEL_ID}@${MODEL_REVISION}"
log "下载到:   ${TARGET}"
log "宿主机余量: $(df -h "$WEIGHT_PARENT" | awk 'NR==2 {print $4}') 可用"

# ------------------------------------------------------------ 本机快速完整性判定
host_weight_bytes() {
    python3 - "$TARGET" <<'PY'
import glob
import json
import os
import sys

d = sys.argv[1]
shards = []
idx = os.path.join(d, "model.safetensors.index.json")
if os.path.isfile(idx):
    try:
        with open(idx, encoding="utf-8") as fh:
            shards = sorted(set(json.load(fh).get("weight_map", {}).values()))
    except Exception:
        shards = []
if not shards:
    shards = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.safetensors")))
total = 0
for shard in shards:
    path = os.path.join(d, shard)
    if os.path.isfile(path):
        total += os.path.getsize(path)
have_cfg = os.path.isfile(os.path.join(d, "config.json"))
print(total if have_cfg and shards else 0)
PY
}

HAVE_BYTES="$(host_weight_bytes || echo 0)"
if [ "$CHECK_ONLY" = "1" ]; then
    if [ "$HAVE_BYTES" -ge "$MIN_BYTES" ] 2>/dev/null; then
        log "权重完整：$(awk -v b="$HAVE_BYTES" 'BEGIN{printf "%.2f GB", b/1e9}')（${TARGET}）"
        exit 0
    fi
    die "权重不完整或缺失：$(awk -v b="$HAVE_BYTES" 'BEGIN{printf "%.2f GB", b/1e9}') < 下限 $(awk -v b="$MIN_BYTES" 'BEGIN{printf "%.2f GB", b/1e9}')，用 bash scripts/fetch_weight.sh 下载"
fi
if [ "$FORCE" != "1" ] && [ "$HAVE_BYTES" -ge "$MIN_BYTES" ] 2>/dev/null; then
    log "已完整（$(awk -v b="$HAVE_BYTES" 'BEGIN{printf "%.2f GB", b/1e9}')），跳过下载。强制重下：--force"
    exit 0
fi

# ------------------------------------------------------------------ 环境与镜像
command -v docker >/dev/null 2>&1 || {
    die "本机没有 docker 命令。
  · 想在宿主机构建镜像：在装了 docker 的宿主机上跑本脚本；
  · 只想下权重：pip install modelscope && modelscope download --model ${MODEL_ID} --local_dir ${TARGET}"
}
docker info >/dev/null 2>&1 || die "docker 守护进程不可用（权限不足？sudo usermod -aG docker \$USER）"

if [ "$NO_BUILD" = "1" ]; then
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "镜像 $IMAGE 不存在，去掉 --no-build 先构建"
    log "跳过构建，使用已有镜像 $IMAGE"
else
    log "构建下载器镜像 $IMAGE（首次约 1 分钟，modelscope 依赖不大）"
    docker build \
        -f docker/Dockerfile.weights \
        -t "$IMAGE" \
        --build-arg MODELSCOPE_VERSION="$MODELSCOPE_VERSION" \
        . || die "镜像构建失败"
fi

# ------------------------------------------------------------------ 运行下载
RUN_ARGS=(run --rm
    -v "${WEIGHT_PARENT}:/weight"
    -e MODEL_ID="$MODEL_ID"
    -e MODEL_REVISION="$MODEL_REVISION"
    -e LOCAL_DIR="/weight/${WEIGHT_BASENAME}"
    -e DOWNLOAD_WORKERS="$DOWNLOAD_WORKERS"
    -e WEIGHT_MIN_BYTES="$MIN_BYTES"
    -e FORCE="$FORCE")
[ -t 1 ] && RUN_ARGS+=(-t)

# 内网代理/镜像站：设了就透传，没设就不传（避免把空值塞进容器）
for var in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy MODELSCOPE_DOMAIN; do
    if [ -n "${!var:-}" ]; then
        RUN_ARGS+=(-e "${var}=${!var}")
        log "透传环境变量 $var"
    fi
done

log "开始下载（21GB，视带宽约 10~60 分钟；中断可重跑续传）"
docker "${RUN_ARGS[@]}" "$IMAGE"

log "完成：${TARGET}"
log "下一步：确保 .env 里 MODEL_PATH=${TARGET}，然后 bash scripts/docker_up.sh --no-weight"
