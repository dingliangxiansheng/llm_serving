"""``/v1/chat/completions``：代理 vLLM，并注入用户隔离的历史与长期记忆。

一次请求的完整链路：

1. 鉴权拿用户（``X-OpenWebUI-User-Id``）→ 2. 解析会话 id → 3. 幂等落库用户提问
→ 4. 检索长期记忆 → 5. 裁剪上下文并注入记忆片段 → 6. 取并发槽位
→ 7. 转发上游（流式原样透传，同时增量解析）→ 8. 落库回答、记用量、投递记忆抽取任务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ..history import build_context, history_header, merge_system
from ..limiter import BusyError, RateLimitError
from ..memory import render_memory_block
from ..services import Services
from ..sse import SSEAccumulator, sse_error
from ..upstream import UpstreamError
from ..utils import (
    content_to_text,
    estimate_messages_tokens,
    sha1,
    truncate,
)
from .deps import CurrentUser, get_services, parse_client_ip, require_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

#: 透传给上游的字段白名单之外不再过滤——vLLM 更新很快，未知字段直接透传更稳。
_DROP_KEYS = {"user"}  # Open WebUI 会带上内部 user 对象，vLLM 不认识


def _error_response(
    message: str,
    status_code: int,
    *,
    code: str = "invalid_request_error",
    retry_after: float | None = None,
) -> JSONResponse:
    headers = {"Retry-After": str(int(max(retry_after, 1)))} if retry_after else None
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": code, "code": code}},
        headers=headers,
    )


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return content_to_text(msg.get("content")).strip()
    return ""


def _resolve_session(
    request: Request, user: CurrentUser, messages: list[dict[str, Any]], services: Services
) -> str:
    """优先用 Open WebUI 的 chat id，保证同一会话多轮落在同一个 session 下。"""
    header_name = services.settings.auth.header_chat_id
    raw = (request.headers.get(header_name) or "").strip() if header_name else ""
    if not raw and user.session_id:
        raw = user.session_id
    if raw:
        return raw[:128]
    seed = _last_user_text(messages)
    return "auto_" + sha1(f"{user.id}\x00{seed[:200]}")[:20]


class _Slot:
    """并发槽位的一次性释放凭证（生成器与 background task 都可能触发）。"""

    __slots__ = ("_limiter", "_released")

    def __init__(self, services: Services) -> None:
        self._limiter = services.limiter
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter.release()


def _record_turn(
    services: Services,
    user: CurrentUser,
    *,
    session_id: str,
    ctx: list[dict[str, Any]],
    model: str,
    status: int,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    assistant_text: str,
    user_text: str,
    stream: bool,
) -> None:
    """回答落库 + 用量记账 + 触发异步记忆抽取。任何异常都不应该影响用户。"""
    try:
        if status == 200 and assistant_text.strip():
            services.history.append(session_id, user.id, "assistant", assistant_text)
        if prompt_tokens <= 0:
            prompt_tokens = estimate_messages_tokens(ctx)
        services.db.log_usage(
            user.id,
            session_id,
            model,
            prompt_tokens,
            completion_tokens,
            latency_ms,
            status,
        )
        services.metrics.inc(
            "chat_requests_total", status=str(status), stream="true" if stream else "false"
        )
        services.metrics.observe_latency(latency_ms / 1000.0)
        services.metrics.add_tokens("prompt", prompt_tokens)
        services.metrics.add_tokens("completion", completion_tokens)
        if (
            status == 200
            and assistant_text.strip()
            and user_text
            and not assistant_text.startswith("[调用工具]")
        ):
            services.extractor.submit(user.id, session_id, user_text, assistant_text)
    except Exception:
        log.exception("本轮记账失败 user=%s session=%s", user.id, session_id)


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> Response:
    services = get_services(request)
    cfg = services.settings

    try:
        payload = await request.json()
    except Exception:
        return _error_response("请求体不是合法 JSON", 400)
    if not isinstance(payload, dict):
        return _error_response("请求体必须是 JSON 对象", 400)

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error_response("messages 不能为空", 400)

    if cfg.limits.daily_token_quota > 0:
        used = services.db.tokens_today(user.id)
        if used >= cfg.limits.daily_token_quota:
            return _error_response(
                f"今日 token 配额已用尽（{used}/{cfg.limits.daily_token_quota}）",
                429,
                code="quota_exceeded",
                retry_after=600,
            )

    user_text = _last_user_text(messages)
    session_id = _resolve_session(request, user, messages, services)
    services.history.ensure_session(
        session_id,
        user.id,
        history_header(messages),
        client=(request.headers.get("user-agent") or "")[:120] or None,
    )
    services.history.append(session_id, user.id, "user", user_text)

    # ---------------------------------------------------------------- 上下文装配
    ctx = build_context(
        messages,
        max_turns=cfg.history.max_turns,
        max_prompt_tokens=cfg.history.max_prompt_tokens,
    )
    memory_hit_count = 0
    if cfg.memory.enabled and user_text:
        hits = services.memory.search(user.id, user_text)
        block = render_memory_block(hits, cfg.memory.max_inject_tokens)
        if block:
            ctx = merge_system(ctx, block)
            services.memory.mark_used([h.id for h in hits])
            memory_hit_count = len(hits)

    upstream_payload: dict[str, Any] = {k: v for k, v in payload.items() if k not in _DROP_KEYS}
    upstream_payload["messages"] = ctx
    upstream_payload["model"] = cfg.upstream.model

    is_stream = bool(payload.get("stream"))
    if is_stream:
        options = dict(payload.get("stream_options") or {})
        options["include_usage"] = True
        upstream_payload["stream_options"] = options
    else:
        upstream_payload.pop("stream_options", None)

    log.info(
        "chat user=%s session=%s stream=%s msgs=%d ctx=%d mem=%d ip=%s",
        user.id,
        session_id,
        is_stream,
        len(messages),
        len(ctx),
        memory_hit_count,
        parse_client_ip(request),
    )

    # ---------------------------------------------------------------- 取槽位
    try:
        await services.limiter.acquire(user.id)
    except RateLimitError as exc:
        return _error_response(
            f"请求过于频繁，请 {exc.retry_after:.0f}s 后再试",
            429,
            code="rate_limited",
            retry_after=exc.retry_after,
        )
    except BusyError as exc:
        return _error_response(
            f"服务繁忙（并发已满），请 {exc.retry_after:.0f}s 后再试",
            503,
            code="server_busy",
            retry_after=exc.retry_after,
        )

    slot = _Slot(services)
    started = time.perf_counter()

    if is_stream:
        return StreamingResponse(
            _stream(
                services,
                user,
                slot=slot,
                payload=upstream_payload,
                ctx=ctx,
                session_id=session_id,
                user_text=user_text,
                started=started,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
            background=BackgroundTask(slot.release),
        )

    try:
        data = await services.upstream.chat(upstream_payload)
    except UpstreamError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        _record_turn(
            services,
            user,
            session_id=session_id,
            ctx=ctx,
            model=cfg.upstream.model,
            status=exc.status_code,
            latency_ms=latency_ms,
            prompt_tokens=0,
            completion_tokens=0,
            assistant_text="",
            user_text=user_text,
            stream=False,
        )
        log.warning("上游失败 status=%s detail=%s", exc.status_code, truncate(exc.detail, 300))
        return _error_response(truncate(exc.detail, 800), exc.status_code, code="upstream_error")
    finally:
        slot.release()

    assistant_text = _assistant_text(data)
    usage = data.get("usage") or {}
    latency_ms = int((time.perf_counter() - started) * 1000)
    _record_turn(
        services,
        user,
        session_id=session_id,
        ctx=ctx,
        model=str(data.get("model") or cfg.upstream.model),
        status=200,
        latency_ms=latency_ms,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        assistant_text=assistant_text,
        user_text=user_text,
        stream=False,
    )
    return JSONResponse(content=data)


async def _stream(
    services: Services,
    user: CurrentUser,
    *,
    slot: _Slot,
    payload: dict[str, Any],
    ctx: list[dict[str, Any]],
    session_id: str,
    user_text: str,
    started: float,
) -> AsyncIterator[bytes]:
    acc = SSEAccumulator()
    status = 200
    try:
        async with services.upstream.stream_chat(payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                status = resp.status_code
                log.warning("流式上游失败 status=%s body=%s", status, truncate(body, 300))
                yield sse_error(truncate(_error_message(body), 800))
                return
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                yield chunk
                acc.feed(chunk.decode("utf-8", "replace"))
    except UpstreamError as exc:
        status = exc.status_code
        log.warning("流式上游异常 %s: %s", status, truncate(exc.detail, 300))
        yield sse_error(truncate(exc.detail, 800))
    except asyncio.CancelledError:
        status = 499  # 客户端主动断开（nginx 记 499）
        raise
    except Exception as exc:
        status = 500
        log.exception("流式代理异常")
        yield sse_error(f"网关异常: {type(exc).__name__}")
    finally:
        acc.flush()
        latency_ms = int((time.perf_counter() - started) * 1000)
        _record_turn(
            services,
            user,
            session_id=session_id,
            ctx=ctx,
            model=str(acc.result.model or services.settings.upstream.model),
            status=status,
            latency_ms=latency_ms,
            prompt_tokens=acc.result.prompt_tokens,
            completion_tokens=acc.result.completion_tokens,
            assistant_text=_stream_assistant_text(acc.result),
            user_text=user_text,
            stream=True,
        )
        slot.release()


def _assistant_text(data: dict[str, Any]) -> str:
    """取回答正文；如果这一轮只调了工具没有正文，落一个可读的标记。

    工具调用轮次的 ``content`` 是空的，如果不记录，``/my/sessions`` 里就会
    少掉一问一答，看起来像丢消息。
    """
    try:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        text = content_to_text(message.get("content")).strip()
        if text:
            return text
        return _tool_call_marker(message.get("tool_calls"))
    except Exception:
        return ""


def _stream_assistant_text(result: Any) -> str:
    """流式场景：正文优先，只有工具调用时退化成标记。"""
    text = (result.content or "").strip()
    if text:
        return text
    names = getattr(result, "tool_names", None) or []
    joined = "; ".join(str(n) for n in names if n)
    return f"[调用工具] {joined}" if joined else ""


def _tool_call_marker(tool_calls: Any) -> str:
    """把 tool_calls 压成一行，仅用于历史展示与审计。"""
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""
    names: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        args = str(fn.get("arguments") or "")
        names.append(f"{name}({truncate(args, 120)})" if name else "")
    joined = "; ".join(n for n in names if n)
    return f"[调用工具] {joined}" if joined else ""


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip() or "上游返回错误"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if error:
            return str(error)
        if data.get("message"):
            return str(data["message"])
    return body.strip() or "上游返回错误"
