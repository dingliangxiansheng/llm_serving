"""接口级测试：用户隔离、历史落库、记忆注入、流式透传、管理接口。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from tests.conftest import chat_payload, headers


def test_health_and_models(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    models = client.get("/v1/models", headers=headers()).json()
    assert models["data"][0]["id"] == "test-model"


def test_requires_api_key_when_configured(tmp_path: Any, upstream: Any) -> None:
    import httpx
    from fastapi.testclient import TestClient as TC
    from tests.conftest import make_settings

    from gateway.app import create_app
    from gateway.config import AuthConfig

    settings = make_settings(tmp_path, auth=AuthConfig(mode="header", api_keys=["k1"]))
    app = create_app(settings, upstream_transport=httpx.MockTransport(upstream.handler))
    with TC(app) as c:
        assert c.get("/v1/models", headers=headers()).status_code == 401
        ok = c.get("/v1/models", headers={**headers(), "Authorization": "Bearer k1"})
        assert ok.status_code == 200


def test_chat_persists_history_and_returns_upstream_payload(
    client: TestClient, upstream: Any
) -> None:
    resp = client.post("/v1/chat/completions", headers=headers(), json=chat_payload("你好"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "收到，记住了。"

    # 上游收到的是网关改写后的 model，且不含 Open WebUI 的内部字段
    assert upstream.calls[-1]["model"] == "test-model"
    assert "user" not in upstream.calls[-1]

    history = client.get("/my/sessions/c1/messages", headers=headers()).json()["data"]
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert history[0]["content"] == "你好"


def test_resent_history_is_deduplicated(client: TestClient) -> None:
    payload = chat_payload("重复的问题")
    client.post("/v1/chat/completions", headers=headers(), json=payload)
    client.post("/v1/chat/completions", headers=headers(), json=payload)
    history = client.get("/my/sessions/c1/messages", headers=headers()).json()["data"]
    # 用户提问只落一条，助手回答两条（内容相同也会被去重成一条）
    assert len([r for r in history if r["role"] == "user"]) == 1


def test_users_are_isolated(client: TestClient) -> None:
    client.post(
        "/v1/chat/completions", headers=headers("alice", "ca"), json=chat_payload("a 的问题")
    )
    client.post("/v1/chat/completions", headers=headers("bob", "cb"), json=chat_payload("b 的问题"))

    alice = client.get("/my/sessions", headers=headers("alice")).json()["data"]
    bob = client.get("/my/sessions", headers=headers("bob")).json()["data"]
    assert {row["id"] for row in alice} == {"ca"}
    assert {row["id"] for row in bob} == {"cb"}
    # 跨用户读会话必须 404
    assert client.get("/my/sessions/cb/messages", headers=headers("alice")).status_code == 404


def test_memory_is_injected_into_system_prompt(client: TestClient, upstream: Any) -> None:
    client.post(
        "/admin/users/u1/memories",
        headers={"X-Admin-Token": "admin-secret"},
        json={"text": "用户负责后端服务，使用 PostgreSQL 15", "kind": "project"},
    )
    client.post(
        "/v1/chat/completions", headers=headers(), json=chat_payload("后端服务用什么数据库？")
    )
    system = upstream.calls[-1]["messages"][0]
    assert system["role"] == "system"
    assert "长期记忆" in system["content"]
    assert "PostgreSQL" in system["content"]


def test_memory_is_not_leaked_between_users(client: TestClient, upstream: Any) -> None:
    client.post(
        "/admin/users/alice/memories",
        headers={"X-Admin-Token": "admin-secret"},
        json={"text": "alice 的服务器密码提示是蓝色", "kind": "fact"},
    )
    client.post("/v1/chat/completions", headers=headers("bob"), json=chat_payload("今天天气如何"))
    messages = upstream.calls[-1]["messages"]
    assert all("蓝色" not in str(m["content"]) for m in messages)


def test_stream_passthrough_and_persistence(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=headers(),
        json=chat_payload("流式问题", stream=True),
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "data: [DONE]" in body
    assert '"content": "你好"' in body or '"content":"你好"' in body

    history = client.get("/my/sessions/c1/messages", headers=headers()).json()["data"]
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "你好，世界"


def test_upstream_error_is_mapped_and_recorded(client: TestClient, upstream: Any) -> None:
    upstream.fail_status = 400
    resp = client.post("/v1/chat/completions", headers=headers(), json=chat_payload("炸一下"))
    assert resp.status_code == 400
    assert "上游炸了" in resp.json()["error"]["message"]
    usage = client.get("/my/usage", headers=headers()).json()
    # 失败也要记账（status=400）：/my/usage 的 days 里应有 1 条请求
    assert usage["days"] and usage["days"][0]["requests"] >= 1
    assert (
        client.get("/admin/overview", headers={"X-Admin-Token": "admin-secret"}).status_code == 200
    )


def test_empty_messages_rejected(client: TestClient) -> None:
    resp = client.post("/v1/chat/completions", headers=headers(), json={"messages": []})
    assert resp.status_code == 400


def test_admin_requires_token(client: TestClient) -> None:
    assert client.get("/admin/overview").status_code == 403
    assert client.get("/admin/users", headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert client.get("/admin/users", headers={"X-Admin-Token": "admin-secret"}).status_code == 200


def test_admin_can_manage_memories(client: TestClient) -> None:
    admin = {"X-Admin-Token": "admin-secret"}
    created = client.post("/admin/users/u9/memories", headers=admin, json={"text": "负责报表平台"})
    assert created.status_code == 200
    memory_id = created.json()["id"]

    listed = client.get("/admin/users/u9/memories", headers=admin).json()
    assert listed["total"] == 1

    assert client.delete(f"/admin/users/u9/memories/{memory_id}", headers=admin).status_code == 200
    assert client.get("/admin/users/u9/memories", headers=admin).json()["total"] == 0


def test_user_can_clear_own_memories(client: TestClient) -> None:
    client.post(
        "/v1/chat/completions", headers=headers(), json=chat_payload("我偏好简洁的回答，别啰嗦")
    )
    client.delete("/my/memories", headers=headers())
    assert client.get("/my/memories", headers=headers()).json()["total"] == 0


def test_daily_quota_blocks_requests(tmp_path: Any, upstream: Any) -> None:
    import httpx
    from fastapi.testclient import TestClient as TC
    from tests.conftest import make_settings

    from gateway.app import create_app
    from gateway.config import LimitsConfig

    settings = make_settings(tmp_path, limits=LimitsConfig(max_concurrent=2, daily_token_quota=1))
    app = create_app(settings, upstream_transport=httpx.MockTransport(upstream.handler))
    with TC(app) as c:
        assert (
            c.post("/v1/chat/completions", headers=headers(), json=chat_payload("一")).status_code
            == 200
        )
        blocked = c.post("/v1/chat/completions", headers=headers(), json=chat_payload("二"))
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "quota_exceeded"
