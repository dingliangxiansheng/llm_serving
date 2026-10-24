"""SSE 增量解析：跨 chunk 的半截 JSON 不能丢。"""

from __future__ import annotations

import json

from gateway.sse import SSEAccumulator, sse_data, sse_error


def _chunk(text: str) -> str:
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


def test_accumulates_content_across_chunks() -> None:
    acc = SSEAccumulator()
    acc.feed(_chunk("你"))
    acc.feed(_chunk("好"))
    assert acc.result.content == "你好"


def test_split_event_across_feeds_is_parsed_once() -> None:
    acc = SSEAccumulator()
    whole = _chunk("完整的一句话")
    acc.feed(whole[:15])  # 半截 JSON
    acc.feed(whole[15:])
    assert acc.result.content == "完整的一句话"


def test_captures_usage_and_finish_reason() -> None:
    acc = SSEAccumulator()
    acc.feed(
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], '
        '"usage": {"prompt_tokens": 7, "completion_tokens": 3}}\n\n'
    )
    acc.feed("data: [DONE]\n\n")
    assert acc.result.finish_reason == "stop"
    assert acc.result.prompt_tokens == 7
    assert acc.result.completion_tokens == 3
    assert acc.result.saw_done is True


def test_malformed_json_is_ignored() -> None:
    acc = SSEAccumulator()
    acc.feed("data: {not json}\n\n")
    acc.feed(_chunk("ok"))
    assert acc.result.content == "ok"


def test_reasoning_delta_is_collected() -> None:
    acc = SSEAccumulator()
    acc.feed('data: {"choices": [{"delta": {"reasoning_content": "想一想"}}]}\n\n')
    assert acc.result.reasoning == "想一想"


def test_flush_handles_trailing_event_without_blank_line() -> None:
    acc = SSEAccumulator()
    acc.feed('data: {"choices": [{"delta": {"content": "尾"}}]}')
    acc.flush()
    assert acc.result.content == "尾"


def test_sse_error_is_terminated_with_done() -> None:
    payload = sse_error("boom").decode()
    assert payload.startswith("data: {")
    assert payload.rstrip().endswith("data: [DONE]")
    assert sse_data({"a": 1}).startswith(b"data: ")
