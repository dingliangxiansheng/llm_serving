#!/usr/bin/env bash
# =============================================================================
# 安装站点署名样式到 Open WebUI
#
# 为什么写 frontend/static/custom.css 而不是 static/custom.css：
#   Open WebUI 每次启动都会清空 open_webui/static/ 再把 open_webui/frontend/static/
#   整个复制过去（open_webui/config.py 的 "Static DIR" 段）。直接改 static/custom.css
#   会在下次启动时被空文件覆盖，所以真正的「源」是 frontend/static/custom.css，
#   前端模板里的 <link rel="stylesheet" href="/static/custom.css"> 最终读到的就是它。
#
# 用法：
#   bash scripts/patch_webui_branding.sh            # 安装到 .venv-webui 里的 Open WebUI
#   bash scripts/patch_webui_branding.sh --check    # 只检查（已装=0，未装=2）
#   bash scripts/patch_webui_branding.sh --restore  # 还原成出厂的空样式
#   bash scripts/patch_webui_branding.sh --target /path/to/open_webui/frontend/static/custom.css
#
# Docker 部署不需要本脚本：docker-compose.yml 已把 custom.css 只读挂载到容器的
# /app/backend/open_webui/frontend/static/custom.css。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT_DIR/webui/branding/custom.css"
MARKER="作者：丁亮"

TARGET=""
MODE="install"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)   MODE="check"; shift ;;
    --restore) MODE="restore"; shift ;;
    --target)  TARGET="${2:?--target 需要路径}"; shift 2 ;;
    -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[x] 未知参数：$1" >&2; exit 1 ;;
  esac
done

log() { printf '[branding] %s\n' "$*"; }

# --- 1. 定位目标文件（frontend/static 优先，其次 build/static、static） ------
if [[ -z "$TARGET" ]]; then
  PY="$ROOT_DIR/.venv-webui/bin/python"
  if [[ -x "$PY" ]]; then
    TARGET="$("$PY" - <<'PY' 2>/dev/null || true
import pathlib

import open_webui

pkg = pathlib.Path(open_webui.__file__).resolve().parent
candidates = [
    pkg / "frontend" / "static" / "custom.css",   # 源码/whl 布局（FROM_INIT_PY=true）
    pkg.parent / "build" / "static" / "custom.css",
    pkg / "static" / "custom.css",
]
for candidate in candidates:
    if candidate.is_file():
        print(candidate)
        break
else:
    print(candidates[0])
PY
)"
  fi
fi

if [[ -z "$TARGET" ]]; then
  echo "[x] 找不到 Open WebUI 的 custom.css。" >&2
  echo "    先建好 .venv-webui（bash scripts/install.sh），或用 --target 指定路径。" >&2
  exit 1
fi

[[ -f "$SRC" ]] || { echo "[x] 缺少源文件：$SRC" >&2; exit 1; }

# --- 2. 各模式处理 ----------------------------------------------------------
case "$MODE" in
  check)
    if [[ -f "$TARGET" ]] && grep -qF "$MARKER" "$TARGET"; then
      log "已安装：$TARGET"
      exit 0
    fi
    log "未安装：$TARGET"
    exit 2
    ;;

  restore)
    if [[ -f "$TARGET.orig" ]]; then
      cp -f "$TARGET.orig" "$TARGET"
      log "已还原：$TARGET（来自 custom.css.orig）"
    else
      # Open WebUI 出厂时这个文件就是空的
      : > "$TARGET"
      log "没有 .orig 备份，已清空：$TARGET"
    fi
    exit 0
    ;;

  install)
    if [[ ! -f "$TARGET" ]]; then
      mkdir -p "$(dirname "$TARGET")"
      : > "$TARGET"
      log "目标不存在，已新建：$TARGET"
    fi

    if grep -qF "$MARKER" "$TARGET"; then
      log "已是最新，跳过：$TARGET"
      exit 0
    fi

    # 只在首次覆盖前备份，避免把已打过补丁的版本存成 .orig
    if [[ ! -f "$TARGET.orig" ]]; then
      cp -f "$TARGET" "$TARGET.orig"
      log "已备份原文件 → $TARGET.orig"
    fi

    cp -f "$SRC" "$TARGET"
    log "已安装署名样式 → $TARGET"
    log "重启 Open WebUI（启动时会把 frontend/static 复制到 static/）或硬刷新浏览器后生效。"
    ;;
esac
