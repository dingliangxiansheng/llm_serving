"""增量解析 vLLM 的 SSE 流，边转发边攒正文/用量。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamResult:
    content: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    saw_done: bool = False
    chunks: int = 0
    tool_names: list[str] = field(default_factory=list)

    @property
    def prompt_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens") or 0)

    @property
    def completion_tokens(self) -> int:
        return int(self.usage.get("completion_tokens") or 0)


class SSEAccumulator:
    """按事件边界累积，跨 chunk 的半截 JSON 不会误判。"""

    def __init__(self) -> None:
        self._buffer = ""
        self.result = StreamResult()

    def feed(self, text: str) -> None:
        self._buffer += text.replace("\r\n", "\n")
        while "\n\n" in self._buffer:
            event, self._buffer = self._buffer.split("\n\n", 1)
            self._handle(event)

    def flush(self) -> None:
        if self._buffer.strip():
            self._handle(self._buffer)
        self._buffer = ""

    def _handle(self, event: str) -> None:
        data_parts = [line[5:].strip() for line in event.split("\n") if line.startswith("data:")]
        if not data_parts:
            return
        data = "\n".join(data_parts)
        if not data or data == "[DONE]":
            self.result.saw_done = True
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return

        self.result.chunks += 1
        if isinstance(chunk.get("usage"), dict):
            self.result.usage.update(chunk["usage"])
        if chunk.get("model"):
            self.result.model = chunk["model"]

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            self._append_reasoning(delta)
            self._append_tool_names(delta)
            content = delta.get("content")
            if isinstance(content, str) and content:
                self.result.content += content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        self.result.content += part["text"]
            if choice.get("finish_reason"):
                self.result.finish_reason = choice["finish_reason"]

    def _append_tool_names(self, delta: dict[str, Any]) -> None:
        """流式工具调用是增量下发的，这里只收集函数名（够写历史标记了）。"""
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = (call.get("function") or {}).get("name") or ""
            if isinstance(name, str) and name and name not in self.result.tool_names:
                self.result.tool_names.append(name)

    def _append_reasoning(self, delta: dict[str, Any]) -> None:
        for key in ("reasoning_content", "reasoning"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                self.result.reasoning += value


def sse_data(payload: dict[str, Any] | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


def sse_error(message: str, *, code: str = "upstream_error") -> bytes:
    return (
        sse_data({"error": {"message": message, "type": code, "code": code}}) + b"data: [DONE]\n\n"
    )
