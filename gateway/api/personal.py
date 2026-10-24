"""``/my/*``：用户自助查看与清理自己的历史、记忆、用量。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..memory import KIND_LABEL
from .deps import CurrentUser, get_services, require_user

router = APIRouter(prefix="/my", tags=["personal"])


@router.get("/profile")
def profile(request: Request, user: CurrentUser = Depends(require_user)) -> dict[str, Any]:
    services = get_services(request)
    row = services.db.user_stats(user.id)
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "stats": dict(row) if row else {},
        "memory_kinds": KIND_LABEL,
    }


@router.get("/sessions")
def my_sessions(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(require_user),
) -> dict[str, Any]:
    services = get_services(request)
    rows = services.history.list_sessions(user.id, limit=limit)
    return {"data": [dict(r) for r in rows]}


@router.get("/sessions/{session_id}/messages")
def my_session_messages(
    request: Request,
    session_id: str,
    user: CurrentUser = Depends(require_user),
) -> dict[str, Any]:
    services = get_services(request)
    rows = services.history.session_messages(session_id, user.id)
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户")
    return {"session_id": session_id, "data": [dict(r) for r in rows]}


@router.delete("/sessions/{session_id}")
def delete_my_session(
    request: Request,
    session_id: str,
    user: CurrentUser = Depends(require_user),
) -> dict[str, Any]:
    services = get_services(request)
    ok = services.history.delete_session(session_id, user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户")
    services.db.audit(user.id, "session.delete", session_id)
    return {"deleted": session_id}


@router.get("/memories")
def my_memories(request: Request, user: CurrentUser = Depends(require_user)) -> dict[str, Any]:
    services = get_services(request)
    return {
        "total": services.memory.count(user.id),
        "data": services.memory.list(user.id),
    }


@router.delete("/memories/{memory_id}")
def delete_my_memory(
    request: Request,
    memory_id: str,
    user: CurrentUser = Depends(require_user),
) -> dict[str, Any]:
    services = get_services(request)
    if not services.memory.delete(user.id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    services.db.audit(user.id, "memory.delete", memory_id)
    return {"deleted": memory_id}


@router.delete("/memories")
def clear_my_memories(
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict[str, Any]:
    services = get_services(request)
    count = services.memory.clear(user.id)
    services.db.audit(user.id, "memory.clear", str(count))
    return {"deleted": count}


@router.get("/usage")
def my_usage(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    user: CurrentUser = Depends(require_user),
) -> dict[str, Any]:
    services = get_services(request)
    return {
        "tokens_today": services.db.tokens_today(user.id),
        "daily_quota": services.settings.limits.daily_token_quota,
        "days": [dict(r) for r in services.db.user_usage(user.id, days=days)],
    }
