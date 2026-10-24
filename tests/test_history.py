"""上下文裁剪与落库幂等。"""

from __future__ import annotations

from pathlib import Path

from gateway.config import HistoryConfig
from gateway.db import Database
from gateway.history import HistoryStore, build_context, history_header, merge_system


def _msgs(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"问题{i}"})
        out.append({"role": "assistant", "content": f"回答{i}"})
    return out


def test_build_context_keeps_system_and_latest_message() -> None:
    messages = [{"role": "system", "content": "系统提示"}, *_msgs(30)]
    ctx = build_context(messages, max_turns=3, max_prompt_tokens=4096)
    assert ctx[0] == {"role": "system", "content": "系统提示"}
    assert ctx[-1] == messages[-1]
    # 3 轮 = 6 条
    assert len(ctx) == 1 + 6


def test_build_context_respects_token_budget() -> None:
    long_text = "很长的内容" * 400
    messages = [
        {"role": "user", "content": long_text},
        {"role": "assistant", "content": long_text},
        {"role": "user", "content": "最后一个问题"},
    ]
    ctx = build_context(messages, max_turns=10, max_prompt_tokens=64)
    assert ctx == [{"role": "user", "content": "最后一个问题"}]


def test_build_context_drops_leading_assistant() -> None:
    messages = [
        {"role": "assistant", "content": "上一轮残留"},
        {"role": "user", "content": "新问题"},
    ]
    ctx = build_context(messages, max_turns=5, max_prompt_tokens=4096)
    assert ctx[0]["role"] == "user"


def test_merge_system_appends_to_existing_system() -> None:
    messages = [{"role": "system", "content": "基础"}, {"role": "user", "content": "hi"}]
    merged = merge_system(messages, "记忆片段")
    assert merged[0]["content"] == "基础\n\n记忆片段"
    assert len(merged) == 2


def test_merge_system_inserts_when_missing() -> None:
    merged = merge_system([{"role": "user", "content": "hi"}], "记忆片段")
    assert merged[0] == {"role": "system", "content": "记忆片段"}
    assert merged[1]["role"] == "user"


def test_history_header_uses_first_user_message() -> None:
    assert history_header([{"role": "user", "content": "帮我看看日志"}]) == "帮我看看日志"
    assert history_header([{"role": "system", "content": "x"}]) == "新会话"


def test_append_is_idempotent_for_resent_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.db")
    db.init_schema()
    store = HistoryStore(db, HistoryConfig())
    store.ensure_session("s1", "u1", "标题")
    assert store.append("s1", "u1", "user", "同一个问题") is True
    # Open WebUI 会重发完整历史，第二次必须被去重
    assert store.append("s1", "u1", "user", "同一个问题") is False
    assert len(store.recent("s1")) == 1
    db.close()


def test_session_messages_are_user_scoped(tmp_path: Path) -> None:
    db = Database(tmp_path / "h2.db")
    db.init_schema()
    store = HistoryStore(db, HistoryConfig())
    store.ensure_session("s2", "u1", "标题")
    store.append("s2", "u1", "user", "只属于 u1")
    assert len(store.session_messages("s2", "u1")) == 1
    assert store.session_messages("s2", "u2") == []
    assert store.delete_session("s2", "u2") is False
    assert store.delete_session("s2", "u1") is True
    db.close()
