#!/usr/bin/env bash
# =============================================================================
# 停止 start_all.sh 拉起的三个进程（只杀本项目路径下的，不动别人的进程）
# 用法：bash scripts/stop_all.sh [--gpu]   --gpu 额外确认显存已释放
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

kill_by_pattern() {  # 名称, 正则
  local name="$1" pat="$2" pids
  # 用 [x] 技巧避免匹配到自己
  pids="$(pgrep -f "$pat" | grep -v "^$$\$" || true)"
  if [[ -z "$pids" ]]; then echo "  $name: 未在运行"; return 0; fi
  echo "  $name: kill $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 20); do
    pgrep -f "$pat" >/dev/null 2>&1 || return 0
    sleep 1
  done
  # 还没退就强制
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
}

echo "[1/3] Open WebUI"
kill_by_pattern "open-webui" "$ROOT/.venv-webui/bin/open-webui"
echo "[2/3] 网关"
kill_by_pattern "gateway"   "$ROOT/.venv-gateway/bin/python"
echo "[3/3] vLLM（会连带 EngineCore 子进程）"
kill_by_pattern "vllm"      "$ROOT/.venv-vllm/bin/vllm serve"

if [[ "${1:-}" == "--gpu" ]]; then
  echo; echo "当前显存："
  nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader
fi
echo "完成。"
