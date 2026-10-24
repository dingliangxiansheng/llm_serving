"""服务容器：把各组件装配在一起，供 ``app`` 与路由层共用。

放在独立模块是为了打断 ``app -> api -> app`` 的循环导入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .config import Settings
from .db import Database
from .extractor import MemoryExtractor
from .history import HistoryStore
from .limiter import Limiter
from .memory import MemoryStore
from .metrics import Metrics
from .upstream import UpstreamClient


@dataclass
class Services:
    settings: Settings
    db: Database
    history: HistoryStore
    memory: MemoryStore
    limiter: Limiter
    upstream: UpstreamClient
    extractor: MemoryExtractor
    metrics: Metrics
    started_at: float


def build_services(
    settings: Settings,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> Services:
    db = Database(settings.storage.db_path)
    db.init_schema()
    memory = MemoryStore(db, settings.memory)
    upstream = UpstreamClient(settings.upstream, transport=upstream_transport)
    return Services(
        settings=settings,
        db=db,
        history=HistoryStore(db, settings.history),
        memory=memory,
        limiter=Limiter(settings.limits),
        upstream=upstream,
        extractor=MemoryExtractor(upstream, memory, settings.memory, settings.upstream.model),
        metrics=Metrics(),
        started_at=time.time(),
    )
