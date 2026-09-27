"""Prime one bounded stream before committing headers, with explicit task ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing

from ..core.errors import RequestCancelledError
from ..schemas.chat import ChatCompletionChunk


class PreparedStream:
    def __init__(self, chunks: AsyncGenerator[ChatCompletionChunk, None]) -> None:
        self.queue: asyncio.Queue[ChatCompletionChunk | Exception | None] = asyncio.Queue(maxsize=1)
        self.first: ChatCompletionChunk | None = None
        self.task = asyncio.create_task(self._produce(chunks), name="chat-stream")
        self.task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        # An upstream generator can cancel itself; wake a consumer awaiting its first token.
        if task.cancelled() and self.queue.empty():
            self.queue.put_nowait(RequestCancelledError("upstream operation was cancelled"))

    @classmethod
    async def open(cls, chunks: AsyncGenerator[ChatCompletionChunk, None]) -> PreparedStream:
        stream = cls(chunks)
        try:
            stream.first = await anext(stream)
        except BaseException:
            await stream.aclose()
            raise
        return stream

    async def _produce(self, chunks: AsyncGenerator[ChatCompletionChunk, None]) -> None:
        try:
            async with aclosing(chunks):
                async for chunk in chunks:
                    await self.queue.put(chunk)
        except Exception as exc:
            await self.queue.put(exc)
        else:
            await self.queue.put(None)

    def __aiter__(self) -> PreparedStream:
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        if self.first is not None:
            chunk, self.first = self.first, None
            return chunk
        if self.task.done() and self.queue.empty():
            if self.task.cancelled():
                raise RequestCancelledError("upstream operation was cancelled")
            raise StopAsyncIteration
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        if isinstance(value, Exception):
            raise value
        return value

    async def aclose(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
