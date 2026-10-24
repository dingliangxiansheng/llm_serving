#!/usr/bin/env bash
# =============================================================================
# 容器/宿主机内一键启动：vLLM → 网关 → Open WebUI（三个进程跑在同一台机器上）
# 用法：bash scripts/start_all.sh [-d]      -d 只启动不等待（不轮询就绪）
# 停止：bash scripts/stop_all.sh
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ ! -f .env ]]; then
  echo "[x] 没有 .env，先 cp .env.example .env 并改 MODEL_PATH / 密钥"; exit 1
fi
set -a; source ./.env; set +a

mkdir -p logs "${DATA_DIR:-./data}" "${HF_CACHE_DIR:-./data/hf}"

WAIT=1
[[ "${1:-}" == "-d" ]] && WAIT=0

VLLM_BIN="$ROOT/.venv-vllm/bin/vllm"
GATEWAY_PY="$ROOT/.venv-gateway/bin/python"
WEBUI_BIN="$ROOT/.venv-webui/bin/open-webui"
for f in "$VLLM_BIN" "$GATEWAY_PY" "$WEBUI_BIN"; do
  [[ -x "$f" ]] || { echo "[x] 缺少 $f，先跑 bash scripts/install.sh"; exit 1; }
done

VLLM_URL="http://127.0.0.1:${VLLM_PORT:-8000}"
GW_URL="http://127.0.0.1:${GATEWAY_PORT:-9000}"
WEB_URL="http://127.0.0.1:${WEBUI_PORT:-8080}"

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3<&-; return 0; } || return 1; }

wait_http() {  # url, timeout_s, name
  local url="$1" timeout="$2" name="$3" i=0
  while (( i < timeout )); do
    if curl -sf -m 3 -o /dev/null "$url"; then echo "  [ok] $name 就绪"; return 0; fi
    sleep 5; i=$((i + 5))
    (( i % 60 == 0 )) && echo "  ... 等待 $name（已 $i s）"
  done
  echo "  [x] $name 在 ${timeout}s 内未就绪，看 logs/${name}.log"; return 1
}

echo "== 1/3 vLLM ($VLLM_URL) =="
if port_busy "${VLLM_PORT:-8000}"; then
  echo "  端口已被占用，跳过启动"
else
  setsid nohup ./scripts/start_vllm.sh > logs/vllm.log 2>&1 < /dev/null &
  echo "  已后台启动，日志 logs/vllm.log（首次加载/图捕获约 10 分钟）"
  if (( WAIT )); then wait_http "$VLLM_URL/v1/models" 1200 vllm || exit 1; fi
fi

echo "== 2/3 网关 ($GW_URL) =="
if port_busy "${GATEWAY_PORT:-9000}"; then
  echo "  端口已被占用，跳过启动"
else
  setsid nohup ./scripts/start_gateway.sh > logs/gateway.log 2>&1 < /dev/null &
  sleep 3
  if (( WAIT )); then wait_http "$GW_URL/healthz" 120 gateway || exit 1; fi
fi

echo "== 3/3 Open WebUI ($WEB_URL) =="
if port_busy "${WEBUI_PORT:-8080}"; then
  echo "  端口已被占用，跳过启动"
else
  setsid nohup ./scripts/start_webui.sh > logs/webui.log 2>&1 < /dev/null &
  echo "  已后台启动，日志 logs/webui.log（首次启动要装/迁移数据库，约 30~60s）"
  if (( WAIT )); then wait_http "$WEB_URL/health" 300 webui || true; fi
fi

cat <<EOF

------------------------------------------------------------
 全部启动完成
 Open WebUI : $WEB_URL
 网关 API   : $GW_URL/v1      （健康检查 $GW_URL/healthz）
 vLLM       : $VLLM_URL/v1
 管理员账号 : ${WEBUI_ADMIN_EMAIL:-admin@example.com} / ${WEBUI_ADMIN_PASSWORD:-见 .env}
 日志       : tail -f logs/vllm.log logs/gateway.log logs/webui.log
 停止       : bash scripts/stop_all.sh
 冒烟       : bash scripts/smoke_test.sh
------------------------------------------------------------
EOF
