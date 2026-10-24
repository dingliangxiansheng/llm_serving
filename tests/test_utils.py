"""工具函数单测：token 估算、多模态拍平、归一化。"""

from __future__ import annotations

from gateway.utils import (
    content_to_text,
    estimate_messages_tokens,
    estimate_tokens,
    normalize_text,
    truncate,
)


def test_estimate_tokens_grows_with_length() -> None:
    assert estimate_tokens("") == 0
    short = estimate_tokens("你好")
    long = estimate_tokens("你好世界，今天天气不错，我们去公园散步吧")
    assert 0 < short < long


def test_estimate_tokens_counts_cjk_heavier_than_latin() -> None:
    # 同样字符数下中文更贵，这是裁剪策略保守的前提
    assert estimate_tokens("中" * 10) > estimate_tokens("a" * 10)


def test_content_to_text_handles_multimodal_parts() -> None:
    content = [
        {"type": "text", "text": "这张图里是什么？"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    assert content_to_text(content) == "这张图里是什么？\n[图片]"


def test_content_to_text_passthrough_for_str_and_none() -> None:
    assert content_to_text("abc") == "abc"
    assert content_to_text(None) == ""


def test_estimate_messages_tokens_adds_per_message_overhead() -> None:
    one = estimate_messages_tokens([{"role": "user", "content": "你好"}])
    two = estimate_messages_tokens(
        [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好"}]
    )
    assert two > one


def test_normalize_text_strips_punct_and_space() -> None:
    assert normalize_text("  后端服务，用 PostgreSQL！ ") == "后端服务用postgresql"


def test_truncate_keeps_short_text_and_marks_long() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdef", 3).endswith("…")
    assert len(truncate("abcdef", 3)) == 3
