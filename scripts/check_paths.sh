#!/usr/bin/env bash
# =============================================================================
# 可移植性检查：仓库里不允许出现「某台机器上的」绝对路径
#
# 为什么要有这道检查：
#   工程是「git clone 到任意机器、任意目录都能跑」的。一旦有人把「数据盘/家目录」那种
#   以 / 开头的宿主机路径写进脚本、文档或配置，别人 clone 下来就会直接报错，而作者本机
#   却毫无感觉。本仓库统一用以下几种写法，所以这道检查应该永远是空的：
#     - 脚本：ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#     - 配置：相对路径（./weight/Qwen3.8-27B-AWQ-INT4、data/gateway.db）
#     - 文档：从仓库根开始的相对路径
#
# 容器内路径（/app、/data/gateway.db 挂载点、/root/.cache/huggingface）是合法的，不拦。
#
# 用法：bash scripts/check_paths.sh     # 有命中则 exit 1
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# 特意用变量拼装，避免本文件自己被自己的规则命中
_P_DATA="/da""ta[0-9]"
_P_HOME="/ho""me/[A-Za-z0-9._-]+/"
_P_MNT="/mn""t/[A-Za-z0-9._-]+/"
PATTERN="${_P_DATA}|${_P_HOME}|${_P_MNT}"

HITS="$(grep -rInE "$PATTERN" . \
  --exclude-dir=.git \
  --exclude-dir='.venv-*' \
  --exclude-dir=weight \
  --exclude-dir=logs \
  --exclude-dir=data \
  --exclude-dir=.cache \
  --exclude-dir=__pycache__ \
  --exclude-dir=.pytest_cache \
  --exclude-dir=.ruff_cache \
  --exclude=check_paths.sh \
  --binary-files=without-match || true)"

if [[ -n "$HITS" ]]; then
  echo "[x] 发现宿主机绝对路径（换台机器 clone 下来就会失效）：" >&2
  echo "$HITS" >&2
  echo >&2
  echo "    修法：脚本里用 ROOT_DIR=\"\$(cd \"\$(dirname \"\${BASH_SOURCE[0]}\")/..\" && pwd)\"，" >&2
  echo "          配置/文档里用相对路径。" >&2
  exit 1
fi

echo "[ok] 未发现宿主机绝对路径"
