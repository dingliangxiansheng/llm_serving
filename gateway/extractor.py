"""异步记忆抽取：不阻塞用户请求，串行调用，失败降级为规则抽取。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from .config import MemoryConfig
from .memory import MemoryStore
from .prompts import build_extraction_messages, heuristic_extract, parse_memories
from .upstream import UpstreamClient, UpstreamError

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExtractionJob:
    user_id: str
    session_id: str
    user_text: str
    assistant_text: str


class MemoryExtractor:
    def __init__(
        self,
        upstream: UpstreamClient,
        memory: MemoryStore,
        cfg: MemoryConfig,
        model: str,
    ) -> None:
        self.upstream = upstream
        self.memory = memory
        self.cfg = cfg
        self.model = model
        self._queue: asyncio.Queue[ExtractionJob] = asyncio.Queue(maxsize=256)
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0
        self.processed = 0

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self.cfg.extract_mode == "off":
            return
        self._task = asyncio.create_task(self._run(), name="memory-extractor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        # 取消是正常关闭路径；任务内部异常已在 _run() 里记过日志，这里只等它退出
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    # ------------------------------------------------------------------ 投递
    def submit(self, user_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
        if self.cfg.extract_mode == "off" or not self.cfg.enabled:
            return
        if len(user_text.strip()) < self.cfg.extract_min_chars:
            return
        job = ExtractionJob(
            user_id=user_id,
            session_id=session_id,
            user_text=user_text[:4000],
            assistant_text=assistant_text[:4000],
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1

    # ------------------------------------------------------------------ 执行
    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
                self.processed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("记忆抽取失败: user=%s", job.user_id)
            finally:
                self._queue.task_done()

    async def _process(self, job: ExtractionJob) -> None:
        if self.cfg.extract_mode == "heuristic":
            items = heuristic_extract(job.user_text, self.cfg.extract_max_memories)
        else:
            items = await self._extract_with_llm(job)

        for item in items:
            created = self.memory.add(
                job.user_id,
                item["text"],
                kind=item.get("kind", "fact"),
                source_session_id=job.session_id,
            )
            if created:
                log.info("写入记忆 user=%s kind=%s", job.user_id, item.get("kind"))

    async def _extract_with_llm(self, job: ExtractionJob) -> list[dict[str, str]]:
        dialogue = f"用户: {job.user_text}\n\n助手: {job.assistant_text}"
        payload = {
            "model": self.model,
            "messages": build_extraction_messages(dialogue, self.cfg.extract_max_memories),
            "temperature": 0.0,
            "max_tokens": 320,
            "stream": False,
        }
        try:
            resp = await self.upstream.chat(payload)
        except UpstreamError as exc:
            log.warning("抽取请求失败(%s)，降级为规则抽取", exc.status_code)
            return heuristic_extract(job.user_text, self.cfg.extract_max_memories)

        try:
            content = resp["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return []
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return parse_memories(content, self.cfg.extract_max_memories)

    def snapshot(self) -> dict[str, int]:
        return {
            "queued": self._queue.qsize(),
            "processed": self.processed,
            "dropped": self.dropped,
        }
