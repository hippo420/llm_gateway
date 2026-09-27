"""Real loopback HTTP reads verify idle timeouts (MockTransport bypasses socket timers)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import pytest

from llm_gateway.adapters.ollama import OllamaAdapter
from llm_gateway.core.errors import (
    UpstreamProtocolError,
    UpstreamReadTimeoutError,
    UpstreamTotalTimeoutError,
)
from llm_gateway.registry.models import TimeoutConfig

from .test_ollama_adapter import _request


@asynccontextmanager
async def upstream(frames):
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            await reader.readexactly(length)
            writer.write(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Content-Type: application/x-ndjson\r\n\r\n"
            )
            await writer.drain()
            for delay, line in frames:
                await asyncio.sleep(delay)
                payload = line.encode() + b"\n"
                writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()
        active = list(tasks)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


async def test_read_timeout_resets_between_chunks(deployment):
    frames = [(0.06, '{"message":{"content":"x"},"done":false}') for _ in range(5)]
    frames.append((0, '{"done":true,"done_reason":"stop"}'))
    async with upstream(frames) as endpoint:
        adapter = OllamaAdapter(
            deployment.model_copy(
                update={
                    "endpoint": endpoint,
                    "timeout": TimeoutConfig(connect=0.2, read=0.2, total=2),
                }
            )
        )
        try:
            chunks = [chunk async for chunk in adapter.stream_chat(_request())]
        finally:
            await adapter.aclose()
    assert len(chunks) == 6  # Full duration exceeds read timeout; each gap stays below it.
    assert chunks[-1].finish_reason == "stop"


async def test_idle_gap_raises_read_timeout_after_partial_response(deployment):
    frames = [(0, '{"message":{"content":"x"},"done":false}'), (0.4, '{"done":true}')]
    async with upstream(frames) as endpoint:
        adapter = OllamaAdapter(
            deployment.model_copy(
                update={
                    "endpoint": endpoint,
                    "timeout": TimeoutConfig(connect=0.1, read=0.1, total=2),
                }
            )
        )
        chunks = []
        try:
            with pytest.raises(UpstreamReadTimeoutError):
                async for chunk in adapter.stream_chat(_request()):
                    chunks.append(chunk)
        finally:
            await adapter.aclose()
    assert [chunk.delta for chunk in chunks] == ["x"]


async def test_total_timeout_does_not_reset_on_arriving_chunks(deployment):
    frames = [(0.06, '{"message":{"content":"x"},"done":false}') for _ in range(20)]
    async with upstream(frames) as endpoint:
        adapter = OllamaAdapter(
            deployment.model_copy(
                update={
                    "endpoint": endpoint,
                    "timeout": TimeoutConfig(connect=0.1, read=0.2, total=0.35),
                }
            )
        )
        try:
            with pytest.raises(UpstreamTotalTimeoutError):
                [chunk async for chunk in adapter.stream_chat(_request())]
        finally:
            await adapter.aclose()


@pytest.mark.parametrize(
    "line", ["[]", "not-json", '{"message":{"content":123}}', '{"done":false}']
)
async def test_bad_payload_or_missing_final_chunk_is_protocol_error(deployment, line):
    async with upstream([(0, line)]) as endpoint:
        adapter = OllamaAdapter(deployment.model_copy(update={"endpoint": endpoint}))
        try:
            with pytest.raises(UpstreamProtocolError):
                [chunk async for chunk in adapter.stream_chat(_request())]
        finally:
            await adapter.aclose()
