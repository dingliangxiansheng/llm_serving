"""提示词模板与解析。"""

from __future__ import annotations

import json
import re
from typing import Any

EXTRACTION_SYSTEM = (
    "你是一个记忆提取器。你的任务是从一段用户与助手的对话中，抽取关于【用户本人】的、"
    "值得长期记住的稳定信息。只输出 JSON 数组，不要输出任何解释或 Markdown 代码块。"
)

EXTRACTION_TEMPLATE = """请从下面的对话中抽取关于用户本人的长期信息。

抽取标准（宁可少抽，不要瞎猜）：
1. 只抽取用户明确表达的内容，不要从助手的话里推断。
2. 只抽取跨会话仍然有用、短期内不会改变的：身份/岗位、技术栈偏好、项目背景、"
   工作习惯、明确要求助手记住的约定。
3. 不要抽取：一次性问题、闲聊、临时情绪、从上下文可推断的琐事、敏感隐私（密码、
   身份证、手机号、住址）。
4. 每条不超过 60 字，用第三人称陈述句（如“用户负责 XX 项目的后端开发”）。
5. 最多 {max_items} 条；没有可抽取的就返回 [].

输出格式（严格）：
[{{"text": "...", "kind": "preference|fact|project|profile"}}]

对话如下：
---
{dialogue}
---
"""

_KIND_ALIASES = {
    "preference": "preference",
    "偏好": "preference",
    "fact": "fact",
    "事实": "fact",
    "project": "project",
    "项目": "project",
    "profile": "profile",
    "画像": "profile",
    "todo": "todo",
    "待办": "todo",
}

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def build_extraction_messages(dialogue: str, max_items: int) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {
            "role": "user",
            "content": EXTRACTION_TEMPLATE.format(dialogue=dialogue, max_items=max_items),
        },
    ]


def parse_memories(raw: str, max_items: int = 3) -> list[dict[str, str]]:
    """宽容解析模型输出，失败返回空列表。"""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out: list[dict[str, str]] = []
    for item in data[:max_items]:
        if isinstance(item, str):
            out.append({"text": item.strip(), "kind": "fact"})
            continue
        if not isinstance(item, dict):
            continue
        text_value = str(item.get("text") or item.get("memory") or "").strip()
        if len(text_value) < 4:
            continue
        kind = str(item.get("kind") or "fact").strip().lower()
        out.append({"text": text_value, "kind": _KIND_ALIASES.get(kind, "fact")})
    return out


# 兜底：模型不可用时用规则抽取“我….”类自述句
_HEURISTIC_RE = re.compile(
    r"(我(?:叫|是|在|负责|主要|喜欢|习惯|从事|做|用|这边|们|的(?:项目|团队|工作))[^。！？\n]{4,60})"
)
_HEURISTIC_KIND = [
    ("喜欢", "preference"),
    ("习惯", "preference"),
    ("用", "preference"),
    ("负责", "project"),
    ("项目", "project"),
    ("团队", "project"),
    ("叫", "profile"),
    ("是", "profile"),
]


def heuristic_extract(text: str, max_items: int = 3) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _HEURISTIC_RE.finditer(text):
        sentence = match.group(1).strip()
        if sentence in seen or len(sentence) < 6:
            continue
        seen.add(sentence)
        kind = next((k for token, k in _HEURISTIC_KIND if token in sentence), "fact")
        out.append({"text": f"用户{sentence}", "kind": kind})
        if len(out) >= max_items:
            break
    return out
