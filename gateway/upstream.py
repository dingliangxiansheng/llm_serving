"""vLLM（OpenAI 兼容）上游客户端。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .config import UpstreamConfig


class UpstreamError(Exception):
    def __init__(self, status_code: int, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retryable = retryable


class UpstreamClient:
    def __init__(
        self,
        cfg: UpstreamConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(cfg.request_timeout_s, connect=cfg.connect_timeout_s),
            transport=transport,
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    # ------------------------------------------------------------------ 流式
    @asynccontextmanager
    async def stream_chat(self, payload: dict[str, Any]) -> AsyncIterator[httpx.Response]:
        request = self._client.build_request(
            "POST", "/chat/completions", json=payload, headers=self._headers()
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:  # 排队/加载超时
            raise UpstreamError(504, f"上游超时: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(502, f"上游不可达: {exc}", retryable=True) from exc
        try:
            yield response
        finally:
            await response.aclose()

    # ------------------------------------------------------------------ 普通
    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(
                "/chat/completions", json=payload, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError(504, f"上游超时: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(502, f"上游不可达: {exc}", retryable=True) from exc
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text[:2000])
        return resp.json()

    async def models(self) -> dict[str, Any]:
        try:
            resp = await self._client.get("/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise UpstreamError(502, f"上游不可达: {exc}", retryable=True) from exc
        else:
            if resp.status_code >= 400:
                raise UpstreamError(resp.status_code, resp.text[:500])
            return resp.json()

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/models", headers=self._headers(), timeout=5.0)
        except httpx.HTTPError:
            return False
        else:
            return resp.status_code < 400
