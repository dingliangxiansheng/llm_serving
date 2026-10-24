#!/usr/bin/env bash
# =============================================================================
# vLLM 容器入口：把环境变量翻译成 vllm serve 参数
# 全部参数均可通过 compose / docker run -e 覆盖，无需改镜像
# =============================================================================
set -euo pipefail

MODEL_PATH="${MODEL_PATH:-/models}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen3.8-27b}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
VLLM_EXTRA_ARGS="${VLLM_EXTRA_ARGS:-}"

log() { echo "[vllm-entrypoint] $*"; }
die() { echo "[vllm-entrypoint][FATAL] $*" >&2; exit 1; }

[ -d "$MODEL_PATH" ] || die "模型目录不存在: $MODEL_PATH（检查 compose 的 MODEL_PATH 挂载）"
[ -f "$MODEL_PATH/config.json" ] || die "模型目录缺少 config.json: $MODEL_PATH"

GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l) || GPU_COUNT=0
[ "$GPU_COUNT" -ge 1 ] || die "容器内看不到 GPU，检查 compose 的 deploy.resources 与宿主机 nvidia-container-toolkit"
if [ "$TENSOR_PARALLEL_SIZE" -gt "$GPU_COUNT" ]; then
    die "TENSOR_PARALLEL_SIZE=$TENSOR_PARALLEL_SIZE 大于可用 GPU 数 $GPU_COUNT"
fi

# 单卡显存预算自检：官方镜像里同样存在被其它进程占用的情况，提前给出人话提示
TOTAL_MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
USED_MIB=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | sort -n | tail -1)
log "模型: $MODEL_PATH"
log "GPU: $GPU_COUNT 张，单卡 total=${TOTAL_MIB}MiB，当前已用(最大)=${USED_MIB}MiB，util=${GPU_MEMORY_UTILIZATION}"
if [ -n "${TOTAL_MIB:-}" ] && [ "$USED_MIB" -gt $((TOTAL_MIB / 2)) ]; then
    log "警告：单卡已用显存超过 50%，若加载失败请降低 GPU_MEMORY_UTILIZATION 或改用 TENSOR_PARALLEL_SIZE=$GPU_COUNT"
fi

set -- serve "$MODEL_PATH" \
    --served-model-name "$SERVED_MODEL_NAME" \
    --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
    --max-model-len "$MAX_MODEL_LEN" \
    --max-num-seqs "$MAX_NUM_SEQS" \
    --host "$VLLM_HOST" \
    --port "$VLLM_PORT"

# shellcheck disable=SC2086  # 需要按空格拆分额外参数
if [ -n "$VLLM_EXTRA_ARGS" ]; then
    set -- "$@" $VLLM_EXTRA_ARGS
fi

log "启动命令: vllm $*"
exec vllm "$@"
