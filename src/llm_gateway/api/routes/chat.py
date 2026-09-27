"""Chat Completions 엔드포인트.

명세: docs/specs/api-spec.md
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from ...core.context import RequestContext
from ...core.errors import GatewayError, InternalError, RequestCancelledError
from ...core.logging import log_event
from ...schemas.chat import ChatCompletionRequest
from ...service.chat_service import ChatService
from ...service.streaming import PreparedStream
from ..dependencies import get_chat_service, get_request_ctx, verify_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(verify_api_key)])

SSE_MEDIA_TYPE = "text/event-stream"
SSE_DONE = "data: [DONE]\n\n"

DEPLOYMENT_HEADER = "X-Gateway-Deployment"
Result = TypeVar("Result")


async def _while_connected(work: Coroutine[Any, Any, Result], request: Request) -> Result:
    """The body has been parsed; watch disconnects even before response headers exist."""

    async def disconnected() -> None:
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    task = asyncio.create_task(work)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            raise RequestCancelledError("client closed the connection")
        return await task
    except BaseException:
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        if isinstance(results[0], PreparedStream):
            await results[0].aclose()
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


class ManagedStreamingResponse(StreamingResponse):
    def __init__(self, stream: PreparedStream, ctx: RequestContext) -> None:
        self.stream = stream
        self._events = _sse(stream, ctx)
        super().__init__(
            self._events, media_type=SSE_MEDIA_TYPE, headers=_gateway_headers(ctx)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # ASGI 2.4 send errors alone cannot detect disconnects while upstream is stalled.
        # Keep one receive watcher for the full response lifetime on every ASGI version.
        sender = asyncio.create_task(self.stream_response(send), name="chat-send")
        watcher = asyncio.create_task(self.listen_for_disconnect(receive), name="chat-disconnect")
        try:
            done, _ = await asyncio.wait((sender, watcher), return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                await sender
            if watcher in done:
                await watcher
        except OSError as exc:
            raise ClientDisconnect() from exc
        finally:
            sender.cancel()
            watcher.cancel()
            await asyncio.gather(sender, watcher, return_exceptions=True)
            try:
                await self._events.aclose()
            finally:
                await self.stream.aclose()
        if self.background is not None:
            await self.background()


@router.post(
    "/chat/completions",
    response_model=None,  # streaming 분기가 있어 자동 스키마를 끈다
    summary="OpenAI-compatible chat completions",
)
@router.post("/chat", response_model=None, include_in_schema=False)  # 별칭
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    service: ChatService = Depends(get_chat_service),
    ctx: RequestContext = Depends(get_request_ctx),
) -> JSONResponse | StreamingResponse:
    # 검증과 deployment 선택은 본문을 흘리기 전에 끝낸다.
    # StreamingResponse 는 헤더를 첫 바이트 전에 확정해야 하기 때문이다.
    deployment = service.prepare(request, ctx, http_request.headers)

    if request.stream:
        stream = await _while_connected(
            PreparedStream.open(service.stream(request, ctx, deployment)),
            http_request,
        )
        return ManagedStreamingResponse(stream, ctx)

    result = await _while_connected(service.complete(request, ctx, deployment), http_request)
    return JSONResponse(
        content=result.response.model_dump(mode="json"),
        headers=_gateway_headers(ctx),
    )


@router.post("/chat/stream", response_model=None, summary="Streaming 강제 별칭")
async def chat_stream(
    request: ChatCompletionRequest,
    http_request: Request,
    service: ChatService = Depends(get_chat_service),
    ctx: RequestContext = Depends(get_request_ctx),
) -> StreamingResponse:
    """stream 값과 무관하게 SSE 로 응답한다."""
    request.stream = True
    deployment = service.prepare(request, ctx, http_request.headers)
    stream = await _while_connected(
        PreparedStream.open(service.stream(request, ctx, deployment)),
        http_request,
    )
    return ManagedStreamingResponse(stream, ctx)


async def _sse(chunks: AsyncIterator, ctx: RequestContext) -> AsyncGenerator[str, None]:
    """chunk iterator -> SSE 텍스트 스트림.

    각 이벤트는 반드시 빈 줄(\\n\\n)로 끝난다. 하나라도 빠지면 클라이언트가 멈춘다.
    """
    try:
        async for chunk in chunks:
            yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
    except asyncio.CancelledError:
        # 클라이언트 이탈. 삼키지 않고 재전파해야 상위 스택이 정리된다.
        raise
    except GatewayError as exc:
        # HTTP 상태는 이미 200 이라 바꿀 수 없다. 에러 chunk 를 보내고 닫는다.
        yield _error_event(exc, ctx)
    except Exception as exc:  # noqa: BLE001 - 스트림을 열어둔 채 끝내지 않기 위한 최후 방어
        log.exception("unhandled error while streaming")
        yield _error_event(InternalError(f"internal error: {exc.__class__.__name__}"), ctx)

    yield SSE_DONE


def _error_event(exc: GatewayError, ctx: RequestContext) -> str:
    """스트림 도중 에러를 SSE 이벤트 한 줄로 만든다 (docs/specs/api-spec.md)."""
    log_event(
        log,
        "chat_stream_failed",
        level=logging.ERROR,
        code=exc.code,
        error_type=str(exc.error_type),
        deployment_id=ctx.deployment_id,
        stream_started=ctx.stream_started,
        detail=exc.detail,
    )
    body = exc.to_error_body(ctx.request_id)
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _gateway_headers(ctx: RequestContext) -> dict[str, str]:
    """Gateway 확장 헤더.

    Phase 6 에서 X-Gateway-Fallback, Phase 7 에서 X-Gateway-Experiment/Variant 가 추가된다.
    """
    headers: dict[str, str] = {}
    if ctx.deployment_id:
        headers[DEPLOYMENT_HEADER] = ctx.deployment_id
    if ctx.fallback_from and ctx.deployment_id:
        headers["X-Gateway-Fallback"] = ctx.deployment_id
    return headers
