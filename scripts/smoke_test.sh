#!/usr/bin/env bash
# =============================================================================
# 冒烟测试：vLLM 就绪 → 网关注册 → 非流式/流式各打一发 → 校验历史与记忆
# 用法：bash scripts/smoke_test.sh [网关地址]
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then set -a; source ./.env; set +a; fi

GW="${1:-http://127.0.0.1:${GATEWAY_PORT:-9000}}"
VLLM="${UPSTREAM_BASE_URL:-http://127.0.0.1:8000/v1}"
MODEL="${SERVED_MODEL_NAME:-qwen3.8-27b}"
USER_ID="${SMOKE_USER_ID:-smoke-user}"
CHAT_ID="${SMOKE_CHAT_ID:-smoke-chat-$(date +%s)}"
H_USER=(-H "X-OpenWebUI-User-Id: $USER_ID" -H "X-OpenWebUI-User-Name: 冒烟测试"
        -H "X-OpenWebUI-User-Email: smoke@example.com" -H "X-OpenWebUI-User-Role: user"
        -H "X-OpenWebUI-Chat-Id: $CHAT_ID" -H "Content-Type: application/json")

hr() { printf '%s\n' "------------------------------------------------------------"; }

hr; echo "[1/6] 等待 vLLM 就绪（最多 15 分钟）"
for i in $(seq 1 90); do
  if curl -sf "$VLLM/models" >/dev/null 2>&1; then echo "  vLLM 就绪"; break; fi
  (( i == 90 )) && { echo "  [x] vLLM 未就绪，查看容器日志"; exit 1; }
  sleep 10
done

hr; echo "[2/6] 网关健康检查"
curl -sf "$GW/healthz" | head -c 400; echo
curl -sf "$GW/readyz" | head -c 400; echo

hr; echo "[3/6] 模型列表"
curl -sf "${H_USER[@]}" "$GW/v1/models" | head -c 400; echo

hr; echo "[4/6] 非流式对话 + 埋一个可被记忆的事实"
ANSWER=$(curl -sf "${H_USER[@]}" "$GW/v1/chat/completions" -d "{
  \"model\": \"$MODEL\",
  \"messages\": [{\"role\":\"user\",\"content\":\"记住：我叫张三，负责的后端服务叫 kd-gateway，用的是 PostgreSQL 15。请简短确认。\"}],
  \"max_tokens\": 120
}")
echo "$ANSWER" | head -c 600; echo

hr; echo "[5/6] 流式对话（观察是否逐块返回）"
curl -sN "${H_USER[@]}" "$GW/v1/chat/completions" -d "{
  \"model\": \"$MODEL\",
  \"messages\": [{\"role\":\"user\",\"content\":\"用一句话说明你收到了上一条信息。\"}],
  \"max_tokens\": 80,
  \"stream\": true
}" | head -c 600; echo

hr; echo "[6/6] 校验落库：会话历史 / 记忆（等记忆抽取完成）"
sleep 8
curl -sf "${H_USER[@]}" "$GW/my/sessions" | head -c 500; echo
curl -sf "${H_USER[@]}" "$GW/my/memories" | head -c 800; echo
curl -sf "${H_USER[@]}" "$GW/my/usage" | head -c 400; echo

hr; echo "冒烟测试结束。若记忆为空：检查 QIANWEN_MEMORY__EXTRACT_MODE 与网关日志。"
