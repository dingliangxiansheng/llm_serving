#!/usr/bin/env bash
# =============================================================================
# 启动 vLLM（OpenAI 兼容 API）
#
# 4090 显存很紧：脚本会先读每张卡的 *空闲* 显存，自动把 --gpu-memory-utilization
# 压到安全值，避免 vLLM 启动半小时后 OOM。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a; source ./.env; set +a
fi

MODEL_PATH="${MODEL_PATH:-weight/Qwen3.8-27B-AWQ-INT4}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen3.8-27b}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-4}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.33}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-4}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_HOST="${VLLM_HOST:-127.0.0.1}"
VLLM_EXTRA_ARGS="${VLLM_EXTRA_ARGS:---enforce-eager}"

[[ -d "$MODEL_PATH" ]] || { echo "[x] 模型目录不存在: $MODEL_PATH" >&2; exit 1; }

VLLM_BIN="$ROOT/.venv-vllm/bin/vllm"
[[ -x "$VLLM_BIN" ]] || VLLM_BIN="$(command -v vllm || true)"
[[ -n "$VLLM_BIN" ]] || { echo "[x] 找不到 vllm，可执行 scripts/install.sh 或激活虚拟环境" >&2; exit 1; }

# 关键：把 vllm 所在目录放进 PATH。
# flashinfer 采样器在首次运行时用 ninja 现场编译 kernel，直接调用 .venv-vllm/bin/vllm
# 而不激活环境时 PATH 里没有 ninja，会报 FileNotFoundError: 'ninja'（500 错误）。
export PATH="$(dirname "$VLLM_BIN"):$PATH"
if ! command -v ninja >/dev/null 2>&1; then
  echo "[!] PATH 里没有 ninja：flashinfer 采样器无法 JIT 编译，可能出现 500。" >&2
  echo "[!] 解决：uv pip install --python $ROOT/.venv-vllm/bin/python ninja" >&2
fi
export VLLM_CACHE_ROOT="${VLLM_CACHE_ROOT:-$ROOT/.cache/vllm}"

# ----------------------------------------------------------------- 显存自适应
read -r GPU_COUNT FREE_MIN TOTAL_MIB <<<"$(
  nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader,nounits |
  awk '{n++; if (min=="" || $1<min) min=$1; if (tot=="") tot=$2}
       END {printf "%d %.0f %d", n, min, tot}'
)"
if (( TENSOR_PARALLEL_SIZE > GPU_COUNT )); then
  echo "[x] 需要 $TENSOR_PARALLEL_SIZE 张卡，实际只有 $GPU_COUNT 张" >&2
  exit 1
fi

SAFE_UTIL=$(awk -v free="$FREE_MIN" -v tot="$TOTAL_MIB" 'BEGIN{printf "%.2f", (free-1024)/tot}')
if awk -v a="$GPU_MEMORY_UTILIZATION" -v b="$SAFE_UTIL" 'BEGIN{exit !(a>b)}'; then
  echo "[!] 期望 utilization=$GPU_MEMORY_UTILIZATION，但单卡最少只剩 ${FREE_MIN}MiB 空闲"
  echo "[!] 自动降到 $SAFE_UTIL（留 1GiB 余量）。若显存被别的进程占用，请先释放再调大。"
  GPU_MEMORY_UTILIZATION="$SAFE_UTIL"
fi

echo "---------------------------------------------"
echo " model      : $MODEL_PATH"
echo " served name: $SERVED_MODEL_NAME"
echo " tp         : $TENSOR_PARALLEL_SIZE (free/GPU=${FREE_MIN}MiB)"
echo " util       : $GPU_MEMORY_UTILIZATION"
echo " max len    : $MAX_MODEL_LEN   max seqs: $MAX_NUM_SEQS"
echo " listen     : $VLLM_HOST:$VLLM_PORT"
echo " extra      : $VLLM_EXTRA_ARGS"
echo "---------------------------------------------"

# shellcheck disable=SC2086
exec "$VLLM_BIN" serve "$MODEL_PATH" \
  --served-model-name "$SERVED_MODEL_NAME" \
  --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --max-model-len "$MAX_MODEL_LEN" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --host "$VLLM_HOST" \
  --port "$VLLM_PORT" \
  $VLLM_EXTRA_ARGS
