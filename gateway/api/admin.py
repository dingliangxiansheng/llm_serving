"""``/admin/*``：运维视角的用户、会话、记忆、用量、审计。

鉴权只用 ``auth.admin_token``（``Authorization: Bearer`` 或 ``X-Admin-Token``），
与用户身份完全分离——避免普通用户读别人的历史。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from .deps import get_services, require_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


@router.get("/overview")
def overview(request: Request) -> dict[str, Any]:
    services = get_services(request)
    db = services.db
    cfg = services.settings
    return {
        "uptime_s": round(time.time() - services.started_at, 1),
        "version": request.app.version,
        "model": cfg.upstream.model,
        "upstream": cfg.upstream.base_url,
        "auth_mode": cfg.auth.mode,
        "concurrency": services.limiter.snapshot(),
        "extractor": services.extractor.snapshot(),
        "counts": {
            "users": db.scalar("SELECT COUNT(*) FROM users") or 0,
            "sessions": db.scalar("SELECT COUNT(*) FROM sessions") or 0,
            "messages": db.scalar("SELECT COUNT(*) FROM messages") or 0,
            "memories": db.scalar("SELECT COUNT(*) FROM memories") or 0,
        },
        "usage_7d": _rows(db.usage_summary(days=7)),
    }


@router.get("/users")
def list_users(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    services = get_services(request)
    return {"data": _rows(services.db.list_users_stats(limit=limit))}


@router.get("/users/{user_id}")
def user_detail(request: Request, user_id: str) -> dict[str, Any]:
    services = get_services(request)
    stats = services.db.user_stats(user_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {
        "user": dict(stats),
        "sessions": _rows(services.history.list_sessions(user_id, limit=50)),
        "memories": services.memory.list(user_id, limit=50),
        "usage": _rows(services.db.user_usage(user_id, days=7)),
    }


@router.get("/users/{user_id}/memories")
def user_memories(request: Request, user_id: str) -> dict[str, Any]:
    services = get_services(request)
    return {
        "total": services.memory.count(user_id),
        "data": services.memory.list(user_id),
    }


@router.post("/users/{user_id}/memories")
def add_memory(
    request: Request,
    user_id: str,
    text: str = Body(..., embed=True),
    kind: str = Body("fact", embed=True),
) -> dict[str, Any]:
    """运维手工补一条记忆（比如“这个人负责 X 项目”），比等模型抽更可靠。"""
    services = get_services(request)
    memory_id = services.memory.add(user_id, text, kind=kind)
    if memory_id is None:
        raise HTTPException(status_code=409, detail="内容为空/过长，或与已有记忆重复")
    services.db.audit("admin", "memory.add", f"{user_id}:{memory_id}")
    return {"id": memory_id}


@router.delete("/users/{user_id}/memories/{memory_id}")
def delete_memory(request: Request, user_id: str, memory_id: str) -> dict[str, Any]:
    services = get_services(request)
    if not services.memory.delete(user_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    services.db.audit("admin", "memory.delete", f"{user_id}:{memory_id}")
    return {"deleted": memory_id}


@router.get("/sessions/{session_id}/messages")
def session_messages(
    request: Request,
    session_id: str,
    user_id: str = Query(..., description="必须显式指定归属用户，避免越权"),
) -> dict[str, Any]:
    services = get_services(request)
    rows = services.history.session_messages(session_id, user_id)
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session_id": session_id, "data": _rows(rows)}


@router.get("/usage")
def usage(request: Request, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    services = get_services(request)
    return {"days": _rows(services.db.usage_summary(days=days))}


@router.get("/audit")
def audit(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    services = get_services(request)
    return {"data": _rows(services.db.recent_audit(limit=limit))}
