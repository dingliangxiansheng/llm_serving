"""零依赖的小工具：时间、ID、token 估算、文本归一化。"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

# 中日韩统一表意文字（含扩展 A 与常用兼容区）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s\u3000-\u303f\uff00-\uffef!-/:-@\[-`{-~]+")

#: 粗略 token 估算系数。中文约 0.7 token/字，英文约 0.3 token/字符。
_CJK_TOKEN_RATIO = 0.7
_LATIN_TOKEN_RATIO = 0.3


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}_{raw}" if prefix else raw


def sha1(text: str) -> str:
    """内容指纹 / 会话 ID 用的短哈希，**不是**安全用途。"""
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def content_to_text(content: Any) -> str:
    """把 OpenAI 的 content 字段拍平成纯文本。

    ``content`` 可能是 str，也可能是 ``[{"type": "text", "text": ...}, ...]``。
    图片/视频部分用占位符表示，仅用于落库与打分，不参与 token 估算。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            itype = item.get("type")
            if itype in {"text", "input_text"}:
                parts.append(str(item.get("text", "")))
            elif itype in {"image_url", "input_image", "image"}:
                parts.append("[图片]")
            elif itype in {"video_url", "video"}:
                parts.append("[视频]")
            elif itype in {"audio", "input_audio"}:
                parts.append("[音频]")
            else:
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(p for p in parts if p)
    return str(content)


def estimate_tokens(text: str) -> int:
    """不加载 tokenizer 的保守估算，用于窗口裁剪与预算控制。

    Qwen 系列 tokenizer 对中文约 1 token/1.4 字，对英文约 1 token/3.5 字符。
    这里取偏大的系数，宁可少放历史也不要超长。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = max(len(text) - cjk, 0)
    return int(cjk * _CJK_TOKEN_RATIO + rest * _LATIN_TOKEN_RATIO) + 4


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    # 每条消息有 role/分隔符开销，统一按 +4 计
    return sum(estimate_tokens(content_to_text(m.get("content"))) + 4 for m in messages)


def normalize_text(text: str) -> str:
    """用于记忆去重的归一化：去标点、去空白、转小写。"""
    lowered = text.strip().lower()
    lowered = _PUNCT_RE.sub("", lowered)
    return _SPACE_RE.sub("", lowered)


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def clip_messages_text(messages: list[dict[str, Any]], limit: int) -> str:
    """把多轮对话拍成可读文本，供记忆抽取使用。"""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        label = {"user": "用户", "assistant": "助手", "system": "系统"}.get(role, role)
        lines.append(f"{label}: {content_to_text(msg.get('content'))}")
    return truncate("\n".join(lines), limit)
