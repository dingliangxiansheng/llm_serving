#!/usr/bin/env bash
# =============================================================================
# 网关容器入口：做最小自检后交给 uvicorn
# 配置全部走 QIANWEN_* 环境变量（见 configs/gateway.yaml 的同名字段）
# =============================================================================
set -euo pipefail

QIANWEN_CONFIG="${QIANWEN_CONFIG:-/app/configs/gateway.yaml}"
export QIANWEN_CONFIG
export QIANWEN_SERVER__HOST="${QIANWEN_SERVER__HOST:-0.0.0.0}"
export QIANWEN_SERVER__PORT="${QIANWEN_SERVER__PORT:-9000}"

log() { echo "[gateway-entrypoint] $*"; }

if [ -f "$QIANWEN_CONFIG" ]; then
    log "配置文件: $QIANWEN_CONFIG"
else
    log "未找到 $QIANWEN_CONFIG，使用内置默认值 + 环境变量"
fi

DB_PATH="${QIANWEN_STORAGE__DB_PATH:-/data/gateway.db}"
mkdir -p "$(dirname "$DB_PATH")"
log "上游: ${QIANWEN_UPSTREAM__BASE_URL:-<yaml 默认>}  模型: ${QIANWEN_UPSTREAM__MODEL:-<yaml 默认>}"
log "认证模式: ${QIANWEN_AUTH__MODE:-<yaml 默认>}  数据库: $DB_PATH"

python - <<'PY'
import sys
from gateway.config import get_settings

cfg = get_settings()
if cfg.auth.mode in {"shared", "off"}:
    print(
        f"[gateway-entrypoint][WARN] auth.mode={cfg.auth.mode}：所有请求会被归为同一个用户，"
        "历史与记忆无法按人隔离，内部多人场景请使用 header 模式",
        file=sys.stderr,
    )
if cfg.server.host not in {"0.0.0.0", "::"}:
    print(
        f"[gateway-entrypoint][WARN] 监听地址 {cfg.server.host} 在容器内不可达，已强制改为 0.0.0.0",
        file=sys.stderr,
    )
PY

exec python -m gateway
