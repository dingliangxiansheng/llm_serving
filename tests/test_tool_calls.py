"""工具调用轮次的落库行为。

Open WebUI 打开"原生函数调用"后会带 tools + tool_choice=auto 请求上游，
此时模型只回 tool_calls、正文为空。如果网关直接把空正文丢给历史表，
/my/sessions 就会少掉一问一答，看起来像丢了消息。
"""

from __future__ import annotations

from gateway.api.chat import _assistant_text, _stream_assistant_text
from gateway.sse import StreamResult


def test_non_stream_tool_call_becomes_marker() -> None:
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
                        }
                    ],
                }
            }
        ]
    }
    text = _assistant_text(data)
    assert text.startswith("[调用工具] get_weather(")
    assert "北京" in text


def test_non_stream_plain_content_wins() -> None:
    data = {"choices": [{"message": {"content": "你好", "tool_calls": None}}]}
    assert _assistant_text(data) == "你好"


def test_stream_result_with_only_tool_names() -> None:
    result = StreamResult(content="", tool_names=["get_weather"])
    assert _stream_assistant_text(result) == "[调用工具] get_weather"


def test_stream_result_prefers_content() -> None:
    result = StreamResult(content=" 你好 ", tool_names=["get_weather"])
    assert _stream_assistant_text(result) == "你好"


def test_stream_accumulator_collects_tool_names() -> None:
    from gateway.sse import SSEAccumulator

    acc = SSEAccumulator()
    acc.feed(
        'data: {"choices":[{"delta":{"tool_calls":'
        '[{"index":0,"function":{"name":"search"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":'
        '[{"index":0,"function":{"arguments":"{}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":'
        '[{"index":1,"function":{"name":"read_file"}}]}}]}\n\n'
        "data: [DONE]\n\n"
    )
    acc.flush()
    assert acc.result.tool_names == ["search", "read_file"]
    assert acc.result.content == ""
