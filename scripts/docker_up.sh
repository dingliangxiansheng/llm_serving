#!/usr/bin/env bash
# =============================================================================
# 一键部署：检查环境 -> 生成 .env -> 下载权重 -> 构建镜像 -> 启动 -> 等待健康
# 用法：
#   ./scripts/docker_up.sh               # 下载/校验权重 + 构建并后台启动
#   ./scripts/docker_up.sh --no-build    # 只启动（镜像已构建过）
#   ./scripts/docker_up.sh --no-weight   # 跳过权重下载（权重已就位时用）
#   ./scripts/docker_up.sh --weight-only # 只下载权重，不构建、不启动
#   ./scripts/docker_up.sh --down        # 停止并删除容器（保留卷）
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

NO_BUILD=0
DOWN=0
NO_WEIGHT=0
WEIGHT_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --no-build) NO_BUILD=1 ;;
        --down) DOWN=1 ;;
        --no-weight) NO_WEIGHT=1 ;;
        --weight-only) WEIGHT_ONLY=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\033[1;32m[docker_up]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[docker_up]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[docker_up][FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 环境检查
command -v docker >/dev/null 2>&1 || die "未安装 docker"
docker info >/dev/null 2>&1 || die "docker 守护进程不可用（当前用户有权限吗？试 sudo usermod -aG docker \$USER）"
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "缺少 docker compose 插件（apt install docker-compose-plugin）"
fi

command -v nvidia-smi >/dev/null 2>&1 || die "宿主机没有 nvidia-smi，无法运行 vLLM"
GPU_COUNT=$(nvidia-smi -L | wc -l)
log "检测到 ${GPU_COUNT} 张 GPU"
if ! docker info 2>/dev/null | grep -qi 'nvidia'; then
    warn "未在 docker info 中看到 nvidia runtime；若启动 vLLM 报 'could not select device driver'，请安装 nvidia-container-toolkit 并重启 docker"
fi

if [ "$DOWN" = "1" ]; then
    log "停止服务（保留数据卷）"
    "${COMPOSE[@]}" down
    exit 0
fi

# ---------------------------------------------------------------- .env
if [ ! -f .env ]; then
    [ -f .env.example ] || die "缺少 .env.example"
    cp .env.example .env
    log "已从 .env.example 生成 .env"
    if command -v openssl >/dev/null 2>&1; then
        SECRET_KEY=$(openssl rand -hex 32)
        ADMIN_TOKEN=$(openssl rand -hex 24)
        GATEWAY_API_KEY="sk-llm-serving-$(openssl rand -hex 12)"
        python3 - "$SECRET_KEY" "$ADMIN_TOKEN" "$GATEWAY_API_KEY" <<'PY'
import pathlib, sys
secret, admin, api_key = sys.argv[1:4]
p = pathlib.Path(".env")
lines = []
for line in p.read_text(encoding="utf-8").splitlines():
    if line.startswith("WEBUI_SECRET_KEY="):
        line = f"WEBUI_SECRET_KEY={secret}"
    elif line.startswith("ADMIN_TOKEN="):
        line = f"ADMIN_TOKEN={admin}"
    elif line.startswith("GATEWAY_API_KEY="):
        line = f"GATEWAY_API_KEY={api_key}"
    lines.append(line)
p.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
        log "已随机生成 WEBUI_SECRET_KEY / ADMIN_TOKEN / GATEWAY_API_KEY（见 .env）"
    else
        warn "没有 openssl，请手工修改 .env 里的 WEBUI_SECRET_KEY 与 ADMIN_TOKEN"
    fi
fi

# shellcheck disable=SC1091
set -a; . ./.env; set +a

# ---------------------------------------------------------------- 权重
# 构建/启动之前先把权重准备好：缺失时用 modelscope 下到 MODEL_PATH（默认 ./weight/<模型名>），
# 已完整则自动跳过。下载器是单独镜像 docker/Dockerfile.weights，不会把 21GB 烤进 vLLM 镜像。
if [ "$NO_WEIGHT" = "0" ]; then
    log "检查模型权重（缺失则用 modelscope 下载，约 21GB）"
    bash "$ROOT_DIR/scripts/fetch_weight.sh" || die "权重准备失败，排查后可用 --no-weight 跳过"
else
    log "按 --no-weight 跳过权重下载"
fi

if [ "$WEIGHT_ONLY" = "1" ]; then
    log "只下权重（--weight-only），到此结束"
    exit 0
fi

[ -n "${MODEL_PATH:-}" ] || die ".env 里 MODEL_PATH 为空"
[ -f "${MODEL_PATH}/config.json" ] || die "MODEL_PATH=${MODEL_PATH} 下没有 config.json，检查权重路径"
[ -n "${WEBUI_SECRET_KEY:-}" ] || die ".env 里 WEBUI_SECRET_KEY 为空"

# ---------------------------------------------------------------- 构建 & 启动
if [ "$NO_BUILD" = "1" ]; then
    log "跳过构建，直接启动"
    "${COMPOSE[@]}" up -d
else
    log "构建镜像并启动（首次拉取 vLLM 基础镜像可能较慢）"
    "${COMPOSE[@]}" up -d --build
fi

# ---------------------------------------------------------------- 等待健康
log "等待 vLLM 加载模型（21GB INT4，通常 2~5 分钟）..."
for i in $(seq 1 180); do
    state=$(docker inspect -f '{{.State.Health.Status}}' llm-serving-vllm 2>/dev/null || echo unknown)
    if [ "$state" = "healthy" ]; then
        log "vLLM 就绪"
        break
    fi
    if [ $((i % 12)) -eq 0 ]; then
        log "  ...仍在加载（${i}0s，状态=$state），查看日志：${COMPOSE[*]} logs -f vllm"
    fi
    sleep 10
done

for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${GATEWAY_PORT:-9000}/readyz" >/dev/null 2>&1; then
        log "网关就绪"
        break
    fi
    sleep 2
done

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
log "完成。访问：http://${HOST_IP:-<服务器IP>}:${WEBUI_PORT:-8080}  （管理员：${WEBUI_ADMIN_EMAIL:-admin@example.com}）"
log "网关管理接口（本机）：http://127.0.0.1:${GATEWAY_PORT:-9000}/admin/overview  （-H 'X-Admin-Token: \$ADMIN_TOKEN'）"
log "查看日志：${COMPOSE[*]} logs -f   |   停止：./scripts/docker_up.sh --down"
