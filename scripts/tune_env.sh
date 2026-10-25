#!/usr/bin/env bash
# =============================================================================
# 按本机 GPU 实测值回写 .env 里和显存相关的四项：
#   TENSOR_PARALLEL_SIZE / GPU_MEMORY_UTILIZATION / MAX_MODEL_LEN / MAX_NUM_SEQS
#
# 为什么需要：.env.example 的默认值来自「4×4090、每卡被占 15GB」那台机器
# （TP=4、util=0.33）。直接拷到单卡 32GB 的机器上，util×显存 < 权重体积，
# vLLM 启动时会报 Not enough memory，而且错误信息完全指不到 .env。
#
# 用法：
#   bash scripts/tune_env.sh              # 计算并写回 .env（首次备份为 .env.bak）
#   bash scripts/tune_env.sh --dry-run    # 只打印，不写文件
#   SKIP_TUNE=1 bash scripts/bootstrap.sh # 完全跳过自动调参，保留 .env 原值
#
# 可覆盖的环境变量：
#   WEIGHT_GIB           权重大小（GiB），默认按 .env 的 MODEL_PATH 里 *.safetensors 实测
#   KV_BYTES_PER_TOKEN   每 token 的 KV 字节数，默认从模型 config.json 推算
#   TARGET_NUM_SEQS      期望并发，默认 2
#   TARGET_UTIL_MAX      显存占用上限，默认 0.92
#   ACTIVATION_GIB       预留给激活/临时张量的显存，默认 2.0
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${ENV_FILE:-.env}"
DRY=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[1;32m[tune_env]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[tune_env][!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[tune_env][FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "$ENV_FILE 不存在：先 cp .env.example .env"
if [ "${SKIP_TUNE:-0}" = "1" ]; then
    log "SKIP_TUNE=1，跳过自动调参（保留 .env 原值）"
    exit 0
fi

# ---------------------------------------------------------------- GPU 实测
if ! command -v nvidia-smi >/dev/null 2>&1; then
    warn "没有 nvidia-smi：无 GPU 环境，保持 .env 原值"
    exit 0
fi
read -r GPU_N TOTAL_MIB FREE_MIB <<<"$(
    nvidia-smi --query-gpu=memory.total,memory.free --format=csv,noheader,nounits 2>/dev/null |
    awk '{n++; if (tot=="" || $1<tot) tot=$1; if (fr=="" || $2<fr) fr=$2}
         END {printf "%d %d %d", n+0, tot+0, fr+0}'
)"
[ "${GPU_N:-0}" -ge 1 ] || { warn "nvidia-smi 没报出 GPU，保持 .env 原值"; exit 0; }
[ "${TOTAL_MIB:-0}" -ge 1024 ] || { warn "显存读数异常（${TOTAL_MIB:-0} MiB），保持 .env 原值"; exit 0; }

# ---------------------------------------------------------------- 权重体积
model_rel="$(sed -n 's/^MODEL_PATH=\(.*\)/\1/p' "$ENV_FILE" | tail -1)"
model_rel="${model_rel:-./weight/Qwen3.8-27B-AWQ-INT4}"
case "$model_rel" in /*) model_dir="$model_rel" ;; *) model_dir="$ROOT/${model_rel#./}" ;; esac

weight_bytes() {
    find "$model_dir" -maxdepth 1 -name '*.safetensors' -type f -printf '%s\n' 2>/dev/null |
    awk '{s+=$1} END{printf "%.0f", s+0}'
}
wb="$(weight_bytes)"
if [ "${wb:-0}" -gt 0 ]; then
    WEIGHT_GIB="${WEIGHT_GIB:-$(awk -v b="$wb" 'BEGIN{printf "%.2f", b/1073741824}')}"
else
    WEIGHT_GIB="${WEIGHT_GIB:-19.57}"
    warn "还没下完权重，按 WEIGHT_GIB=${WEIGHT_GIB} 估算（下完重跑本脚本可校准）"
fi

# ---------------------------------------------------------------- KV 体积
# 每 token KV = 2(K+V) × kv_heads × head_dim × 2字节 × 全注意力层数
# Qwen3.5 是混合注意力（64 层里只有 16 层 full_attention），不能按层数全算
kv_heads=4; head_dim=256; full_layers=64
if [ -f "$model_dir/config.json" ] && command -v python3 >/dev/null 2>&1; then
    read -r kv_heads head_dim full_layers <<<"$(
        python3 - "$model_dir/config.json" <<'PY' 2>/dev/null || echo "4 256 64"
import json, sys
c = json.load(open(sys.argv[1]))
tc = c.get("text_config", c)
kvh = tc.get("num_key_value_heads") or c.get("num_key_value_heads") or 4
hd = tc.get("head_dim") or 256
lt = tc.get("layer_types") or []
full = lt.count("full_attention") if lt else (tc.get("num_hidden_layers") or 64)
print(kvh, hd, full)
PY
    )"
fi
KV_BYTES_PER_TOKEN="${KV_BYTES_PER_TOKEN:-$(( 2 * kv_heads * head_dim * 2 * full_layers ))}"

ACTIVATION_GIB="${ACTIVATION_GIB:-2.0}"
TARGET_NUM_SEQS="${TARGET_NUM_SEQS:-2}"
TARGET_UTIL_MAX="${TARGET_UTIL_MAX:-0.92}"
MAX_LEN_CAP="${MAX_LEN_CAP:-32768}"   # 超过这个上下文长度收益递减、显存也会被吃满

# ---------------------------------------------------------------- 计算
NEW_TP="" NEW_UTIL="" NEW_LEN="" NEW_SEQ=""
read -r NEW_TP NEW_UTIL NEW_LEN NEW_SEQ <<<"$(
    awk -v gpus="$GPU_N" -v tot="$TOTAL_MIB" -v free="$FREE_MIB" \
        -v wgib="$WEIGHT_GIB" -v kvt="$KV_BYTES_PER_TOKEN" -v kvh="$kv_heads" \
        -v act="$ACTIVATION_GIB" -v nseq="$TARGET_NUM_SEQS" -v umax="$TARGET_UTIL_MAX" \
        -v cap="$MAX_LEN_CAP" '
    BEGIN {
        totg = tot / 1024.0
        # util：以「当前真的空闲多少」为准，上限 umax，下限 0.50（再低权重也放不下，交给 TP）
        u = (free - 1024) / tot
        if (u > umax) u = umax
        if (u < 0.50) u = 0.50

        # TP：从 1 开始找「最小能装下」的并发度（TP 越小通信越少、越快）
        # 约束：≤ GPU 数，且必须整除 num_kv_heads（GQA 的硬性要求）
        tpmax = gpus
        if (kvh > 0 && kvh < tpmax) tpmax = kvh
        tp = 0
        for (t = 1; t <= tpmax; t *= 2) {
            if (u * totg - wgib / t >= act) { tp = t; break }
        }
        if (tp == 0) tp = tpmax        # 怎么都不够：取最大，让 vLLM 自己报明确错误

        left = u * totg - wgib / tp - act   # 留给 KV cache 的 GiB
        if (left < 0.25) left = 0.25
        maxlen = int(left * 1073741824 / (kvt * nseq))
        maxlen = int(maxlen / 8192) * 8192         # 向下取到 8k 的整数倍
        if (maxlen > cap) maxlen = cap
        if (maxlen < 8192) { maxlen = 8192; nseq = 1 }
        printf "%d %.2f %d %d", tp, u, maxlen, nseq
    }')"

# ---------------------------------------------------------------- 写回
old_of() { sed -n "s/^$1=\(.*\)/\1/p" "$ENV_FILE" | tail -1; }

changed=0
summary=""
plan_line() {  # key new old
    local k="$1" new="$2" old="$3"
    if [ "$new" = "$old" ]; then
        summary+="  $(printf '%-24s %s（无需修改）' "$k" "$new")"$'\n'
    else
        summary+="  $(printf '%-24s %s → %s' "$k" "${old:-<未设置>}" "$new")"$'\n'
        changed=1
    fi
}
plan_line TENSOR_PARALLEL_SIZE      "$NEW_TP"   "$(old_of TENSOR_PARALLEL_SIZE)"
plan_line GPU_MEMORY_UTILIZATION    "$NEW_UTIL" "$(old_of GPU_MEMORY_UTILIZATION)"
plan_line MAX_MODEL_LEN             "$NEW_LEN"  "$(old_of MAX_MODEL_LEN)"
plan_line MAX_NUM_SEQS              "$NEW_SEQ"  "$(old_of MAX_NUM_SEQS)"

printf '%s' "$summary"
log "依据：GPU ${GPU_N} 张 × $((TOTAL_MIB/1024)) GiB（空闲 ${FREE_MIB} MiB） | 权重 ${WEIGHT_GIB} GiB | KV ${KV_BYTES_PER_TOKEN} B/token（${full_layers} 层全注意力）"

if [ "$DRY" = "1" ]; then
    log "--dry-run：未写入 $ENV_FILE"
    exit 0
fi
if [ "$changed" = "0" ]; then
    log "$ENV_FILE 已是本机最优值，未改动"
    exit 0
fi

[ -f "$ENV_FILE.bak" ] || cp "$ENV_FILE" "$ENV_FILE.bak"
set_kv() {
    local k="$1" v="$2"
    if grep -qE "^${k}=" "$ENV_FILE"; then
        sed -i "s|^${k}=.*|${k}=${v}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$k" "$v" >>"$ENV_FILE"
    fi
}
set_kv TENSOR_PARALLEL_SIZE "$NEW_TP"
set_kv GPU_MEMORY_UTILIZATION "$NEW_UTIL"
set_kv MAX_MODEL_LEN "$NEW_LEN"
set_kv MAX_NUM_SEQS "$NEW_SEQ"

# 网关的 prompt 上限必须小于 MAX_MODEL_LEN（留出回答空间），否则上游 400
gpt="$(old_of GATEWAY_MAX_PROMPT_TOKENS)"
if [ -n "$gpt" ] && [ "$gpt" -ge "$NEW_LEN" ] 2>/dev/null; then
    new_gpt=$(( NEW_LEN * 3 / 4 / 512 * 512 ))
    set_kv GATEWAY_MAX_PROMPT_TOKENS "$new_gpt"
    log "GATEWAY_MAX_PROMPT_TOKENS $gpt → $new_gpt（必须小于 MAX_MODEL_LEN）"
fi

log "已写入 $ENV_FILE（原值备份在 $ENV_FILE.bak）。重启服务后生效：bash scripts/start_all.sh"
