"""跨会话长期记忆：抽取、去重、检索、注入。

检索不用向量库，也不用 FTS5——中文短词（"后端"）在 FTS5 的 trigram 下匹配不到。
这里用「中文字 + 字符二元组 + 英文单词」的倒排打分（IDF 加权覆盖度），
纯 Python、零依赖、可单测，几百条记忆的规模下足够快。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from .config import MemoryConfig
from .db import Database
from .utils import estimate_tokens, new_id, normalize_text, utcnow, utcnow_iso

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_SEGMENT_RE = re.compile(r"[。！？；;!?\n\r\t]+")
_CJK_START = 0x3400


def _is_cjk(ch: str) -> bool:
    return bool(ch) and ord(ch[0]) >= _CJK_START


def terms(text: str) -> list[str]:
    """切词：英文/数字整词，中文单字 + 相邻二元组。"""
    if not text:
        return []
    lowered = text.lower()
    out: list[str] = []
    prev_cjk_end = -1
    for match in _WORD_RE.finditer(lowered):
        token = match.group()
        out.append(token)
        if len(token) == 1 and _is_cjk(token):
            if (
                match.start() == prev_cjk_end
                and match.start() > 0
                and _is_cjk(lowered[match.start() - 1])
            ):
                out.append(lowered[match.start() - 1 : match.end()])
            prev_cjk_end = match.end()
        else:
            prev_cjk_end = -1
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def query_segments(query: str) -> list[set[str]]:
    """把提问按标点切成若干句，返回每句的词集合。

    为什么要切：用户一句话里常同时问几件事（"我叫什么？负责哪个服务？"），
    整句算覆盖度会把只对上一件事的短记忆稀释到阈值以下，导致该记起的没记起。
    切句后按"最相关的那一句"打分，长提问就不会淹没短记忆。
    """
    segments: list[set[str]] = []
    for raw in _SEGMENT_RE.split(query or ""):
        seg = set(terms(raw))
        if seg:
            segments.append(seg)
    if not segments:
        whole = set(terms(query or ""))
        if whole:
            segments.append(whole)
    return segments


@dataclass(slots=True)
class MemoryHit:
    id: str
    text: str
    kind: str
    score: float
    weight: float


class MemoryStore:
    def __init__(self, db: Database, cfg: MemoryConfig) -> None:
        self.db = db
        self.cfg = cfg

    # ------------------------------------------------------------------ 写
    def add(
        self,
        user_id: str,
        text: str,
        kind: str = "fact",
        source_session_id: str | None = None,
    ) -> str | None:
        """写入一条记忆；重复或近似重复则只加权，返回 None。"""
        text = " ".join(text.split()).strip()
        if len(text) < 4 or len(text) > 500:
            return None
        norm = normalize_text(text)
        if not norm:
            return None
        self.db.ensure_user(user_id)

        existing = self.db.query(
            "SELECT id, text, weight FROM memories WHERE user_id = ?", (user_id,)
        )
        new_terms = set(terms(text))
        for row in existing:
            if row["text"] == text or normalize_text(row["text"]) == norm:
                self.db.execute(
                    "UPDATE memories SET weight = weight + 0.5, hits = hits + 1 WHERE id = ?",
                    (row["id"],),
                )
                return None
        for row in existing:
            if jaccard(new_terms, set(terms(row["text"]))) >= self.cfg.dedup_similarity:
                return None

        mem_id = new_id("mem")
        try:
            self.db.execute(
                """
                INSERT INTO memories
                    (id, user_id, text, norm, kind, weight, hits,
                     source_session_id, created_at)
                VALUES (?, ?, ?, ?, ?, 1.0, 0, ?, ?)
                """,
                (mem_id, user_id, text, norm, kind, source_session_id, utcnow_iso()),
            )
        except Exception:
            return None
        self.prune(user_id)
        return mem_id

    def delete(self, user_id: str, memory_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)
        )
        return cur.rowcount > 0

    def clear(self, user_id: str) -> int:
        cur = self.db.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        return cur.rowcount

    def prune(self, user_id: str) -> None:
        total = self.count(user_id)
        if total <= self.cfg.max_per_user:
            return
        rows = self.db.query(
            "SELECT id FROM memories WHERE user_id = ? ORDER BY weight ASC, created_at ASC LIMIT ?",
            (user_id, total - self.cfg.max_per_user),
        )
        for row in rows:
            self.db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))

    # ------------------------------------------------------------------ 读
    def count(self, user_id: str) -> int:
        return int(
            self.db.scalar("SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,)) or 0
        )

    def list(self, user_id: str, limit: int = 200) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in rows]

    def search(
        self,
        user_id: str,
        query: str,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[MemoryHit]:
        top_k = self.cfg.inject_top_k if top_k is None else top_k
        min_score = self.cfg.min_score if min_score is None else min_score
        rows = self.db.query(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, self.cfg.max_per_user),
        )
        if not rows:
            return []

        segments = query_segments(query)
        if not segments:
            return []
        all_terms: set[str] = set().union(*segments)

        doc_terms = [set(terms(r["text"])) for r in rows]
        total = len(rows)
        df: Counter[str] = Counter()
        for doc in doc_terms:
            df.update(doc)

        idf = {t: math.log((total + 1) / (df.get(t, 0) + 1)) + 1.0 for t in all_terms}
        # 每句单独算分母：只跟这一句自己的词量比，长提问不会拉低命中句的得分
        segment_denoms = [sum(idf.get(t, 1.0) for t in seg) or 1.0 for seg in segments]

        now = utcnow()
        hits: list[MemoryHit] = []
        for row, doc in zip(rows, doc_terms, strict=True):
            coverage = 0.0
            for seg, denom in zip(segments, segment_denoms, strict=True):
                matched = seg & doc
                if not matched:
                    continue
                coverage = max(coverage, sum(idf[t] for t in matched) / denom)
            if coverage <= 0.0:
                continue
            age_days = self._age_days(row["created_at"], now)
            recency = 0.5 ** (age_days / max(self.cfg.recency_half_life_days, 1.0))
            score = coverage * (0.75 + 0.25 * recency) * min(float(row["weight"]), 3.0)
            if score >= min_score:
                hits.append(
                    MemoryHit(
                        id=row["id"],
                        text=row["text"],
                        kind=row["kind"],
                        score=round(score, 4),
                        weight=float(row["weight"]),
                    )
                )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def mark_used(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        now = utcnow_iso()
        self.db.executemany(
            "UPDATE memories SET hits = hits + 1, last_used_at = ? WHERE id = ?",
            [(now, mid) for mid in memory_ids],
        )

    @staticmethod
    def _age_days(created_at: str, now: datetime) -> float:
        try:
            created = datetime.fromisoformat(created_at)
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            return 0.0
        return max((now - created).total_seconds() / 86400.0, 0.0)


KIND_LABEL = {
    "preference": "偏好",
    "fact": "事实",
    "project": "项目",
    "todo": "待办",
    "profile": "画像",
}


def render_memory_block(hits: list[MemoryHit], max_tokens: int) -> str:
    """把命中的记忆渲染成可注入的系统提示片段。"""
    if not hits:
        return ""
    lines: list[str] = []
    used = 0
    for hit in hits:
        line = f"- [{KIND_LABEL.get(hit.kind, hit.kind)}] {hit.text}"
        cost = estimate_tokens(line)
        if used + cost > max_tokens and lines:
            break
        lines.append(line)
        used += cost
    if not lines:
        return ""
    return (
        "【用户长期记忆】以下是该用户在历史对话中留下的、可能仍然有效的信息"
        "（按相关度排序；若与本次对话冲突，一律以本次对话为准，不要向用户复述这份清单）：\n"
        + "\n".join(lines)
    )
