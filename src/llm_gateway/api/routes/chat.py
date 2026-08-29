"""Chat Completions 엔드포인트.

명세: docs/specs/api-spec.md
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...core.context import RequestContext
from ...core.errors import GatewayError, InternalError
from ...core.logging import log_event
from ...schemas.chat import ChatCompletionRequest
from ...service.chat_service import ChatService
from ..dependencies import get_chat_service, get_request_ctx, verify_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(verify_api_key)])

SSE_MEDIA_TYPE = "text/event-stream"
SSE_DONE = "data: [DONE]\n\n"

DEPLOYMENT_HEADER = "X-Gateway-Deployment"


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
    deployment = service.prepare(request, ctx)

    if request.stream:
        return StreamingResponse(
            _sse(service.stream(request, ctx, deployment), ctx),
            media_type=SSE_MEDIA_TYPE,
            headers=_gateway_headers(ctx),
        )

    result = await service.complete(request, ctx, deployment)
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
    deployment = service.prepare(request, ctx)
    return StreamingResponse(
        _sse(service.stream(request, ctx, deployment), ctx),
        media_type=SSE_MEDIA_TYPE,
        headers=_gateway_headers(ctx),
    )


async def _sse(chunks: AsyncIterator, ctx: RequestContext) -> AsyncIterator[str]:
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
    return headers
