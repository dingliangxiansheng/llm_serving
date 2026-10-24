"""并发闸门与每用户限流。

4090 的 KV cache 很紧，多人同时长上下文请求最容易把服务打爆，
所以在网关入口做「全局并发上限 + 排队超时 + 每分钟请求数」三道约束。

注意：流式响应必须在**返回 StreamingResponse 之前**拿到槽位，否则 429 无法正确回给客户端，
所以这里暴露显式的 ``acquire`` / ``release``，而不是只用 ``async with``。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .config import LimitsConfig


class BusyError(Exception):
    """并发槽位排队超时。"""

    def __init__(self, retry_after: float = 5.0) -> None:
        super().__init__("server busy")
        self.retry_after = retry_after


class RateLimitError(Exception):
    """超过每分钟请求数。"""

    def __init__(self, retry_after: float = 10.0) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class QuotaExceededError(Exception):
    """超过每日 token 配额。"""


class Limiter:
    def __init__(self, cfg: LimitsConfig) -> None:
        self.cfg = cfg
        self._sem = asyncio.Semaphore(cfg.max_concurrent)
        self._windows: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self.inflight = 0
        self.waiting = 0

    async def _check_rpm(self, user_id: str) -> None:
        if self.cfg.rpm_per_user <= 0:
            return
        now = time.monotonic()
        async with self._lock:
            window = self._windows.setdefault(user_id, deque())
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) >= self.cfg.rpm_per_user:
                retry_after = max(60.0 - (now - window[0]), 1.0)
                raise RateLimitError(retry_after=round(retry_after, 1))
            window.append(now)

    async def acquire(self, user_id: str) -> None:
        await self._check_rpm(user_id)
        self.waiting += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.cfg.queue_timeout_s)
        except TimeoutError as exc:
            raise BusyError(retry_after=max(self.cfg.queue_timeout_s / 10, 3.0)) from exc
        finally:
            self.waiting -= 1
        self.inflight += 1

    def release(self) -> None:
        self.inflight = max(self.inflight - 1, 0)
        self._sem.release()

    @asynccontextmanager
    async def slot(self, user_id: str) -> AsyncIterator[None]:
        await self.acquire(user_id)
        try:
            yield
        finally:
            self.release()

    def snapshot(self) -> dict[str, int]:
        return {
            "max_concurrent": self.cfg.max_concurrent,
            "inflight": self.inflight,
            "waiting": self.waiting,
        }
