"""SQLite 访问层：单连接 + WAL + 写锁，够用且不引入 ORM。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .utils import utcnow_iso

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Database:
    """轻量连接封装。

    uvicorn 单进程多协程场景下，一个 ``check_same_thread=False`` 的连接加写锁，
    比连接池更省资源；多 worker 时靠 WAL + busy_timeout 兜底。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")

    # ------------------------------------------------------------------ 基础
    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # ------------------------------------------------------------------ 用户
    def upsert_user(
        self,
        user_id: str,
        email: str | None = None,
        name: str | None = None,
        role: str | None = None,
    ) -> sqlite3.Row:
        now = utcnow_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, name, role, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    email        = COALESCE(excluded.email, users.email),
                    name         = COALESCE(excluded.name, users.name),
                    role         = COALESCE(excluded.role, users.role),
                    last_seen_at = excluded.last_seen_at
                """,
                (user_id, email, name, role or "user", now, now),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:  # UNIQUE 冲突后仍取不到，说明库坏了
            raise RuntimeError(f"upsert_user 后查不到用户：{user_id}")
        return row

    def get_user(self, user_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def ensure_user(self, user_id: str) -> None:
        """不存在就建一条占位用户。

        历史/记忆表对 users 有外键；运维手工给“还没登录过的人”补记忆时，
        不应该因为缺少用户行而失败。
        """
        now = utcnow_iso()
        self.execute(
            "INSERT OR IGNORE INTO users (id, role, created_at, last_seen_at) "
            "VALUES (?, 'user', ?, ?)",
            (user_id, now, now),
        )

    def list_users(self, limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def user_stats(self, user_id: str) -> sqlite3.Row | None:
        return self.query_one(
            """
            SELECT u.id,
                   u.name,
                   u.email,
                   (SELECT COUNT(*) FROM sessions  s WHERE s.user_id = u.id) AS sessions,
                   (SELECT COUNT(*) FROM messages  m WHERE m.user_id = u.id) AS messages,
                   (SELECT COUNT(*) FROM memories  mm WHERE mm.user_id = u.id) AS memories,
                   (SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0)
                      FROM usage_log ul WHERE ul.user_id = u.id) AS tokens
            FROM users u WHERE u.id = ?
            """,
            (user_id,),
        )

    def list_users_stats(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT u.id, u.name, u.email, u.role, u.last_seen_at,
                   (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS sessions,
                   (SELECT COUNT(*) FROM messages m WHERE m.user_id = u.id) AS messages,
                   (SELECT COUNT(*) FROM memories mm WHERE mm.user_id = u.id) AS memories,
                   (SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0)
                      FROM usage_log ul WHERE ul.user_id = u.id) AS tokens
            FROM users u
            ORDER BY COALESCE(u.last_seen_at, u.created_at) DESC
            LIMIT ?
            """,
            (limit,),
        )

    # ------------------------------------------------------------------ 用量
    def log_usage(
        self,
        user_id: str,
        session_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        status: int = 200,
    ) -> None:
        self.execute(
            """
            INSERT INTO usage_log
                (user_id, session_id, model, prompt_tokens, completion_tokens,
                 latency_ms, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                session_id,
                model,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                status,
                utcnow_iso(),
            ),
        )

    def tokens_today(self, user_id: str) -> int:
        row = self.query_one(
            """
            SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total
            FROM usage_log
            WHERE user_id = ? AND date(created_at) = date('now')
            """,
            (user_id,),
        )
        return int(row["total"]) if row else 0

    def usage_summary(self, days: int = 7) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT date(created_at) AS day,
                   COUNT(*) AS requests,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms
            FROM usage_log
            WHERE created_at >= datetime('now', ?)
            GROUP BY day ORDER BY day DESC
            """,
            (f"-{int(days)} days",),
        )

    def user_usage(self, user_id: str, days: int = 7) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT date(created_at) AS day,
                   COUNT(*) AS requests,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens
            FROM usage_log
            WHERE user_id = ? AND created_at >= datetime('now', ?)
            GROUP BY day ORDER BY day DESC
            """,
            (user_id, f"-{int(days)} days"),
        )

    # ------------------------------------------------------------------ 审计
    def audit(self, user_id: str, action: str, detail: str = "") -> None:
        self.execute(
            "INSERT INTO audit_log (user_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (user_id, action, detail, utcnow_iso()),
        )

    def recent_audit(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
