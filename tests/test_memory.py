"""长期记忆：切词、去重、检索排序、注入渲染。"""

from __future__ import annotations

from pathlib import Path

from gateway.config import MemoryConfig
from gateway.db import Database
from gateway.memory import MemoryStore, render_memory_block, terms


def _store(tmp_path: Path, **overrides) -> tuple[Database, MemoryStore]:
    db = Database(tmp_path / "m.db")
    db.init_schema()
    cfg = MemoryConfig(**{"min_score": 0.0, **overrides})
    return db, MemoryStore(db, cfg)


def test_terms_emits_cjk_unigrams_and_bigrams() -> None:
    out = terms("后端服务")
    assert "后" in out and "后端" in out and "端服" in out and "服务" in out


def test_terms_splits_latin_words() -> None:
    assert "postgresql" in terms("用 PostgreSQL 15 部署")


def test_search_matches_two_char_chinese_query(tmp_path: Path) -> None:
    # FTS5 trigram 在 2 字查询下会失效，这里必须能命中
    db, store = _store(tmp_path)
    store.add("u1", "用户负责后端服务，使用 PostgreSQL 15")
    hits = store.search("u1", "后端")
    assert hits and "后端" in hits[0].text
    db.close()


def test_search_isolated_between_users(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    store.add("u1", "u1 喜欢用 Rust 写服务")
    assert store.search("u2", "Rust") == []
    db.close()


def test_duplicate_add_only_boosts_weight(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    assert store.add("u1", "偏好简洁的回答") is not None
    assert store.add("u1", "偏好简洁的回答") is None
    assert store.count("u1") == 1
    db.close()


def test_near_duplicate_is_skipped(tmp_path: Path) -> None:
    db, store = _store(tmp_path, dedup_similarity=0.8)
    store.add("u1", "负责 kd-gateway 后端服务")
    store.add("u1", "负责 kd-gateway 后端服务，")
    assert store.count("u1") == 1
    db.close()


def test_prune_keeps_max_per_user(tmp_path: Path) -> None:
    db, store = _store(tmp_path, max_per_user=2)
    for i in range(5):
        store.add("u1", f"第{i}条互不相同的记忆内容 xyz{i}")
    assert store.count("u1") <= 2
    db.close()


def test_render_memory_block_respects_token_budget(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    store.add("u1", "负责后端服务")
    hits = store.search("u1", "后端")
    block = render_memory_block(hits, max_tokens=10)
    assert block  # 至少能放一条
    assert "长期记忆" in block
    assert render_memory_block([], 100) == ""
    db.close()


def test_mark_used_bumps_hits(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    mem_id = store.add("u1", "喜欢 Python 异步编程")
    assert mem_id
    store.mark_used([mem_id])
    row = db.query_one("SELECT hits, last_used_at FROM memories WHERE id = ?", (mem_id,))
    assert row is not None and row["hits"] == 1 and row["last_used_at"]
    db.close()


def test_multi_sentence_query_does_not_dilute_short_memory(tmp_path: Path) -> None:
    """一句话里问多件事时，只对上其中一件的短记忆也必须被召回。

    曾经的问题：整句一起算覆盖度，两个问题加起来的词把短记忆的得分压到
    min_score 以下，模型于是"忘记"了用户名字。现在按句切分取最大值。
    """
    _db, store = _store(tmp_path)
    store.add("u1", "用户名叫张三", "profile")
    store.add("u1", "用户负责后端服务 kd-gateway", "project")

    hits = store.search("u1", "我叫什么名字？我负责哪个后端服务？", top_k=5, min_score=0.12)
    texts = [h.text for h in hits]
    assert "用户名叫张三" in texts
    assert any("kd-gateway" in t for t in texts)


def test_query_segments_splits_on_punctuation() -> None:
    from gateway.memory import query_segments

    segs = query_segments("我叫什么？负责哪个服务；用的什么数据库")
    assert len(segs) == 3
    assert all(segs)
