"""FastAPI 应用工厂。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, chat, health, models, personal
from .config import Settings, get_settings
from .services import Services, build_services

VERSION = "1.0.0"

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=getattr(logging, cfg.server.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        services: Services = build_services(cfg, upstream_transport)
        app.state.services = services
        await services.extractor.start()
        log.info(
            "网关启动 port=%s upstream=%s model=%s db=%s",
            cfg.server.port,
            cfg.upstream.base_url,
            cfg.upstream.model,
            cfg.storage.db_path,
        )
        try:
            yield
        finally:
            await services.extractor.stop()
            await services.upstream.aclose()
            services.db.close()

    app = FastAPI(
        title="Qwen3.8 内网网关",
        version=VERSION,
        description="OpenAI 兼容代理：用户隔离、会话历史、长期记忆、限流",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if request.url.path not in {"/healthz", "/metrics"}:
            log.info(
                "%s %s -> %s %.0fms",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
        response.headers["X-Gateway-Version"] = VERSION
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"网关内部错误: {type(exc).__name__}",
                    "type": "internal_error",
                }
            },
        )

    app.include_router(chat.router)
    app.include_router(models.router)
    app.include_router(personal.router)
    app.include_router(admin.router)
    app.include_router(health.router)
    return app


app = create_app()
