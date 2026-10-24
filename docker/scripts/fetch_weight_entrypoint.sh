#!/usr/bin/env bash
# =============================================================================
# 容器入口：用 modelscope 下载模型权重到一个已挂载的目录（通常是宿主机 ./weight）
#
# 环境变量：
#   MODEL_ID          模型仓库，默认 cyankiwi/Qwen3.8-27B-AWQ-INT4
#   MODEL_REVISION    分支/标签/commit，默认 master
#   LOCAL_DIR         下载目录（容器内路径，需已 bind-mount），默认 /weight/<模型名>
#   DOWNLOAD_WORKERS  并发下载线程，默认 8
#   WEIGHT_MIN_BYTES  完整性判定的最小权重字节数，默认 19e9（INT4 约 20GB）
#   FORCE             1 = 忽略已存在文件，强制重新下载
#   VERIFY_ONLY       1 = 只校验现有文件，不下载（可用作部署前自检）
#
# 幂等：目录已完整时直接跳过；中断后重跑会从已下载的部分文件继续（modelscope 逐文件续传）。
# =============================================================================
set -euo pipefail

MODEL_ID="${MODEL_ID:-cyankiwi/Qwen3.8-27B-AWQ-INT4}"
MODEL_REVISION="${MODEL_REVISION:-master}"
LOCAL_DIR="${LOCAL_DIR:-/weight/$(basename "$MODEL_ID")}"
DOWNLOAD_WORKERS="${DOWNLOAD_WORKERS:-8}"
WEIGHT_MIN_BYTES="${WEIGHT_MIN_BYTES:-19000000000}"
FORCE="${FORCE:-0}"
VERIFY_ONLY="${VERIFY_ONLY:-0}"

log() { echo "[weights] $*"; }
warn() { echo "[weights][WARN] $*" >&2; }
die() { echo "[weights][FATAL] $*" >&2; exit 1; }

# ------------------------------------------------------------------ 完整性校验
# 判定标准（不靠猜）：config.json / tokenizer_config.json 存在；index 里列出的每个
# safetensors 分片都存在且不是半截文件（>=100MB）；分片总字节数达到下限。
verify_dir() {
  python3 - "$LOCAL_DIR" "$WEIGHT_MIN_BYTES" <<'PY'
import glob
import json
import os
import sys

d = sys.argv[1]
min_bytes = int(sys.argv[2])
missing: list[str] = []

for name in ("config.json", "tokenizer_config.json"):
    if not os.path.isfile(os.path.join(d, name)):
        missing.append(name)

shards: list[str] = []
index_path = os.path.join(d, "model.safetensors.index.json")
if os.path.isfile(index_path):
    try:
        with open(index_path, encoding="utf-8") as fh:
            weight_map = json.load(fh).get("weight_map", {})
        shards = sorted(set(weight_map.values()))
    except Exception as exc:  # noqa: BLE001 - 损坏的 index 也要报出来
        missing.append(f"model.safetensors.index.json(损坏: {exc})")
else:
    missing.append("model.safetensors.index.json")

if not shards:  # 没有 index 时退化成按后缀找，至少能判定「有没有下到东西」
    shards = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.safetensors")))

total = 0
partial: list[str] = []
for shard in shards:
    path = os.path.join(d, shard)
    if not os.path.isfile(path):
        missing.append(shard)
        continue
    size = os.path.getsize(path)
    total += size
    if size < 100 * 1024 * 1024:  # 单个分片约 4GB，小于 100MB 基本是残包
        partial.append(f"{shard}({size / 1e6:.1f}MB)")

if not shards:
    missing.append("*.safetensors")

problems = missing + ([f"分片疑似未下完: {', '.join(partial)}"] if partial else [])
if total and total < min_bytes:
    problems.append(f"权重合计 {total / 1e9:.2f}GB 小于下限 {min_bytes / 1e9:.2f}GB")

if problems:
    print("INCOMPLETE: " + "; ".join(problems))
    sys.exit(1)
print(f"OK shards={len(shards)} total={total / 1e9:.2f}GB")
PY
}

# ------------------------------------------------------------------ 前置检查
mkdir -p "$LOCAL_DIR" 2>/dev/null || true
[ -d "$LOCAL_DIR" ] || die "下载目录不可用: $LOCAL_DIR（是否忘了 -v 挂载？）"
[ -w "$LOCAL_DIR" ] || die "下载目录不可写: $LOCAL_DIR（检查宿主机目录权限/uid）"

log "模型:     ${MODEL_ID}@${MODEL_REVISION}"
log "目标目录: ${LOCAL_DIR}"
log "磁盘余量: $(df -h "$LOCAL_DIR" | awk 'NR==2 {print $4 " 可用（挂载点 " $6 "）"}')"

if verify_dir >/tmp/verify.log 2>&1; then
  log "已完整：$(cat /tmp/verify.log)"
  if [ "$FORCE" != "1" ] || [ "$VERIFY_ONLY" = "1" ]; then
    log "跳过下载（要强制重下：FORCE=1 或 scripts/fetch_weight.sh --force）"
    exit 0
  fi
  log "FORCE=1，重新下载覆盖"
else
  log "现有内容不完整：$(cat /tmp/verify.log)"
  if [ "$VERIFY_ONLY" = "1" ]; then
    die "校验失败（VERIFY_ONLY=1，不执行下载）"
  fi
fi

command -v modelscope >/dev/null 2>&1 || die "镜像里没有 modelscope CLI，重新构建：docker build -f docker/Dockerfile.weights ."

# ------------------------------------------------------------------ 下载
ARGS=(download --model "$MODEL_ID" --revision "$MODEL_REVISION"
      --local_dir "$LOCAL_DIR" --max-workers "$DOWNLOAD_WORKERS")
[ "$FORCE" = "1" ] && ARGS+=(--force)

log "开始下载：modelscope ${ARGS[*]}"
START=$(date +%s)
modelscope "${ARGS[@]}"
log "下载结束，用时 $(( $(date +%s) - START ))s"

# ------------------------------------------------------------------ 下载后校验
if ! verify_dir >/tmp/verify.log 2>&1; then
  die "下载后校验失败：$(cat /tmp/verify.log)。可重跑本命令续传（已下文件不会重复下载）"
fi
log "校验通过：$(cat /tmp/verify.log)"
log "目录大小: $(du -sh "$LOCAL_DIR" | awk '{print $1}')"
log "宿主机上把 .env 的 MODEL_PATH 指向该目录即可（相对路径 ./weight/$(basename "$LOCAL_DIR") 亦可）"
