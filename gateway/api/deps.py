"""鉴权与依赖注入。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from ..services import Services


@dataclass(slots=True)
class CurrentUser:
    id: str
    email: str | None = None
    name: str | None = None
    role: str = "user"
    session_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role in {"admin", "superuser"}


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:  # 理论上只会在 lifespan 之外被调用
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return services


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def require_user(request: Request) -> CurrentUser:
    services = get_services(request)
    cfg = services.settings

    token = _bearer(request) or request.headers.get("x-api-key", "")
    if cfg.auth.api_keys and token not in cfg.auth.api_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if cfg.auth.mode == "off":
        user_id = cfg.auth.default_user_id
        email = name = None
        role = "admin"
    elif cfg.auth.mode == "shared":
        user_id = cfg.auth.default_user_id
        email = None
        name = cfg.auth.default_user_name
        role = "user"
    else:  # header
        user_id = (request.headers.get(cfg.auth.header_user_id) or "").strip()
        email = (request.headers.get(cfg.auth.header_user_email) or "").strip() or None
        name = (request.headers.get(cfg.auth.header_user_name) or "").strip() or None
        role = (request.headers.get(cfg.auth.header_user_role) or "user").strip()
        if not user_id:
            user_id = cfg.auth.default_user_id
            name = name or cfg.auth.default_user_name
            role = "user"
    if role not in {"admin", "user", "pending"}:
        role = "user"

    services.db.upsert_user(user_id, email=email, name=name, role=role)
    session_id = (request.headers.get(cfg.auth.header_chat_id) or "").strip() or None
    return CurrentUser(id=user_id, email=email, name=name, role=role, session_id=session_id)


def require_admin(request: Request) -> None:
    services = get_services(request)
    admin_token = services.settings.auth.admin_token
    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail="未配置 auth.admin_token，管理接口已禁用（QIANWEN_AUTH__ADMIN_TOKEN）",
        )
    supplied = _bearer(request) or request.headers.get("x-admin-token", "")
    if supplied != admin_token:
        raise HTTPException(status_code=403, detail="管理口令无效")


def parse_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"
