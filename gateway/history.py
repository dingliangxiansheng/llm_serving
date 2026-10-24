"""会话历史：落库（用户隔离）与上下文窗口裁剪。

设计要点
--------
* Open WebUI 每次都把**完整历史**重发给后端，所以写库必须做幂等去重：
  唯一索引 ``(session_id, role, content_hash)`` + 只在“与最近一条同角色消息不同”时插入。
* 上下文裁剪在网关侧做，客户端传多少不影响上游显存；超长历史按 token 预算从旧往新丢。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .config import HistoryConfig
from .db import Database
from .utils import (
    content_to_text,
    estimate_messages_tokens,
    estimate_tokens,
    new_id,
    sha1,
    utcnow_iso,
)


# --------------------------------------------------------------------- 纯函数
def merge_system(messages: list[dict[str, Any]], extra_system: str) -> list[dict[str, Any]]:
    """把 ``extra_system`` 并入系统提示，已有 system 就追加，没有就插到最前。"""
    if not extra_system:
        return messages
    out = [dict(m) for m in messages]
    for msg in out:
        if msg.get("role") == "system":
            base = content_to_text(msg.get("content"))
            msg["content"] = (base + "\n\n" + extra_system).strip() if base else extra_system
            return out
    return [{"role": "system", "content": extra_system}, *out]


def build_context(
    messages: list[dict[str, Any]],
    *,
    max_turns: int,
    max_prompt_tokens: int,
) -> list[dict[str, Any]]:
    """保留系统提示 + 最近 ``max_turns`` 轮，再按 token 预算从旧到新裁剪。

    最后一条消息（当前提问）永远保留。
    """
    system = [m for m in messages if m.get("role") == "system"]
    dialog = [m for m in messages if m.get("role") != "system"]

    if max_turns > 0:
        dialog = dialog[-max_turns * 2 :]

    kept: list[dict[str, Any]] = []
    budget = max_prompt_tokens - estimate_messages_tokens(system)
    for msg in reversed(dialog):
        cost = estimate_tokens(content_to_text(msg.get("content"))) + 4
        if kept and budget - cost < 0:
            break
        kept.append(msg)
        budget -= cost

    kept.reverse()
    if not kept and dialog:
        kept = [dialog[-1]]

    # 裁剪后如果第一条是 assistant，去掉它以免出现“无根”回复
    while len(kept) > 1 and kept[0].get("role") == "assistant":
        kept.pop(0)

    return [*system, *kept]


def history_header(messages: list[dict[str, Any]]) -> str:
    """从首条用户消息里取一段作为会话标题。"""
    for msg in messages:
        if msg.get("role") == "user":
            text = content_to_text(msg.get("content")).strip()
            if text:
                return text[:60]
    return "新会话"


# --------------------------------------------------------------------- 存储
class HistoryStore:
    def __init__(self, db: Database, cfg: HistoryConfig) -> None:
        self.db = db
        self.cfg = cfg

    def ensure_session(
        self, session_id: str, user_id: str, title: str | None = None, client: str | None = None
    ) -> None:
        self.db.ensure_user(user_id)
        now = utcnow_iso()
        self.db.execute(
            """
            INSERT INTO sessions (id, user_id, title, client, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                title = COALESCE(sessions.title, excluded.title)
            """,
            (session_id, user_id, title, client, now, now),
        )

    def append(
        self,
        session_id: str,
        user_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        """幂等追加一条消息；返回是否真的写入。"""
        if not self.cfg.enabled:
            return False
        text = content_to_text(content)
        if not text.strip():
            return False
        digest = sha1(f"{role}\x00{text}")
        last = self.db.query_one(
            "SELECT content_hash FROM messages WHERE session_id = ? AND role = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id, role),
        )
        if last is not None and last["content_hash"] == digest:
            return False
        try:
            self.db.execute(
                """
                INSERT INTO messages
                    (id, session_id, user_id, role, content, tokens, content_hash, created_at, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("msg"),
                    session_id,
                    user_id,
                    role,
                    text,
                    estimate_tokens(text),
                    digest,
                    utcnow_iso(),
                    None,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        self.db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (utcnow_iso(), session_id)
        )
        return True

    def recent(self, session_id: str, limit: int = 50) -> list[sqlite3.Row]:
        rows = self.db.query(
            "SELECT * FROM messages WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))

    def list_sessions(self, user_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.query(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
            FROM sessions s WHERE s.user_id = ?
            ORDER BY s.updated_at DESC LIMIT ?
            """,
            (user_id, limit),
        )

    def session_messages(self, session_id: str, user_id: str) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM messages WHERE session_id = ? AND user_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (session_id, user_id),
        )

    def delete_session(self, session_id: str, user_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
        )
        return cur.rowcount > 0
