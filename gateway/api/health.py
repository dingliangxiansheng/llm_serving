"""健康检查与指标。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .deps import get_services

router = APIRouter(tags=["ops"])


@router.get("/healthz")
def healthz(request: Request) -> dict:
    """存活探针：不依赖上游，容器 healthcheck 用这个。"""
    services = get_services(request)
    return {
        "status": "ok",
        "version": request.app.version,
        "uptime_s": round(time.time() - services.started_at, 1),
    }


@router.get("/readyz")
async def readyz(request: Request) -> Response:
    """就绪探针：真的去探一下 vLLM。"""
    services = get_services(request)
    ok = await services.upstream.health()
    body = {
        "status": "ok" if ok else "degraded",
        "upstream": services.settings.upstream.base_url,
        "model": services.settings.upstream.model,
        "concurrency": services.limiter.snapshot(),
        "extractor": services.extractor.snapshot(),
        "database": str(services.settings.storage.db_path),
    }
    return JSONResponse(status_code=200 if ok else 503, content=body)


@router.get("/metrics")
def metrics(request: Request) -> Response:
    services = get_services(request)
    gauges = {
        "inflight": float(services.limiter.inflight),
        "waiting": float(services.limiter.waiting),
        "extractor_queue": float(services.extractor.snapshot()["queued"]),
        "uptime_seconds": float(time.time() - services.started_at),
    }
    return Response(
        content=services.metrics.render(gauges),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
