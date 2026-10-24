"""pytest 公共夹具：内存库 + Mock 上游 + TestClient。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import (
    AuthConfig,
    HistoryConfig,
    LimitsConfig,
    MemoryConfig,
    Settings,
    StorageConfig,
    UpstreamConfig,
)


def make_settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "upstream": UpstreamConfig(base_url="http://upstream.test/v1", model="test-model"),
        "auth": AuthConfig(mode="header", admin_token="admin-secret"),
        "history": HistoryConfig(max_turns=10, max_prompt_tokens=4096),
        "memory": MemoryConfig(extract_mode="heuristic", min_score=0.05),
        "limits": LimitsConfig(max_concurrent=2, queue_timeout_s=5, rpm_per_user=0),
        "storage": StorageConfig(db_path=str(tmp_path / "gw.db")),
    }
    base.update(overrides)
    return Settings(**base)


class FakeUpstream:
    """记录收到的请求体，按需返回非流式 JSON 或 SSE 流。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_status: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        self.calls.append(payload)
        if self.fail_status is not None:
            return httpx.Response(
                self.fail_status, json={"error": {"message": "上游炸了", "type": "x"}}
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": []})
        if payload.get("stream"):
            events = (
                b"".join(
                    b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n"
                    for chunk in [
                        {"choices": [{"delta": {"content": "你好"}}], "model": "test-model"},
                        {"choices": [{"delta": {"content": "，世界"}}], "model": "test-model"},
                        {
                            "choices": [{"delta": {}, "finish_reason": "stop"}],
                            "model": "test-model",
                            "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                        },
                    ]
                )
                + b"data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=events
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "收到，记住了。"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def client(tmp_path: Any, upstream: FakeUpstream) -> Iterator[TestClient]:
    settings = make_settings(tmp_path)
    app = create_app(settings, upstream_transport=httpx.MockTransport(upstream.handler))
    with TestClient(app) as test_client:
        yield test_client


def headers(user_id: str = "u1", chat_id: str = "c1", role: str = "user") -> dict[str, str]:
    return {
        "X-OpenWebUI-User-Id": user_id,
        "X-OpenWebUI-User-Email": f"{user_id}@example.com",
        "X-OpenWebUI-User-Name": user_id,
        "X-OpenWebUI-User-Role": role,
        "X-OpenWebUI-Chat-Id": chat_id,
    }


def chat_payload(text: str = "你好", *, stream: bool = False, history: list | None = None) -> dict:
    messages = list(history or [])
    messages.append({"role": "user", "content": text})
    return {"model": "whatever", "messages": messages, "stream": stream}
