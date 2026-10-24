#!/usr/bin/env bash
# =============================================================================
# 把本机部署的 Qwen3.8-27B 接进 Claude Code（终端 或 VS Code 扩展）
#
# 原理：vLLM 0.31 原生提供 Anthropic Messages API（/v1/messages），
#       Claude Code 只要把 ANTHROPIC_BASE_URL 指过去就能用，不需要任何转换代理。
#
# 用法：
#   bash scripts/claude_code_env.sh              # 打印环境变量（自己 eval）
#   bash scripts/claude_code_env.sh --run        # 设好环境后直接启动 claude
#   bash scripts/claude_code_env.sh --code       # 启动 VS Code（已导出的环境对扩展生效）
#
# 注意：这里直连 vLLM:8000，**不经过网关**，所以没有按人隔离的历史/记忆/配额。
#       要那些能力请走网关（见 docs/clients.md 的“进阶”一节）。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then set -a; source ./.env; set +a; fi

HOST="${CLAUDE_HOST:-127.0.0.1}"          # 容器内用 127.0.0.1；从别的机器连改成宿主机 IP
PORT="${VLLM_PORT:-8000}"
MODEL="${SERVED_MODEL_NAME:-qwen3.8-27b}"

cat <<EOF
export ANTHROPIC_BASE_URL="http://${HOST}:${PORT}"
export ANTHROPIC_AUTH_TOKEN="local-$(printf '%s' "$MODEL" | tr -cd 'a-z0-9')"   # vLLM 不校验，占位即可
export ANTHROPIC_MODEL="${MODEL}"
export ANTHROPIC_SMALL_FAST_MODEL="${MODEL}"   # 后台小任务（起标题/总结）也用同一个模型
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${MODEL}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${MODEL}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1   # 别去连 Anthropic 的遥测/更新
EOF

case "${1:-}" in
  --run)
    export ANTHROPIC_BASE_URL="http://${HOST}:${PORT}"
    export ANTHROPIC_AUTH_TOKEN="local-${MODEL//[^a-z0-9]/}"
    export ANTHROPIC_MODEL="$MODEL" ANTHROPIC_SMALL_FAST_MODEL="$MODEL"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL" ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL"
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    exec claude "${@:2}"
    ;;
  --code)
    export ANTHROPIC_BASE_URL="http://${HOST}:${PORT}"
    export ANTHROPIC_AUTH_TOKEN="local-${MODEL//[^a-z0-9]/}"
    export ANTHROPIC_MODEL="$MODEL" ANTHROPIC_SMALL_FAST_MODEL="$MODEL"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL" ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL"
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    exec code "${@:2}"
    ;;
esac
