"""Chat Completions 엔드포인트.

명세: docs/specs/api-spec.md
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.context import RequestContext
from ...schemas.chat import ChatCompletionRequest, ChatCompletionResponse
from ...service.chat_service import ChatService
from ..dependencies import get_chat_service, get_request_ctx, verify_api_key

router = APIRouter(tags=["chat"], dependencies=[Depends(verify_api_key)])

SSE_MEDIA_TYPE = "text/event-stream"
SSE_DONE = "data: [DONE]\n\n"


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
) -> ChatCompletionResponse | StreamingResponse:
    """TODO: 구현.

      if request.stream:
          return StreamingResponse(
              _sse(service.stream(request, ctx)),
              media_type=SSE_MEDIA_TYPE,
              headers=_gateway_headers(ctx),
          )
      result = await service.complete(request, ctx)
      -> 응답 헤더에 X-Gateway-Deployment 를 넣어야 하므로
         JSONResponse 로 감싸거나 Response 객체를 주입받아 헤더를 세팅한다.

    주의: StreamingResponse 는 헤더를 **본문 전송 시작 전에** 확정해야 한다.
          deployment 를 스트림 시작 후에 알 수 있는 구조로 만들면 헤더를 못 넣는다.
          -> service 쪽에서 deployment 선택을 먼저 끝내고 iterator 를 만들 것.
    """
    raise NotImplementedError


@router.post("/chat/stream", response_model=None, summary="Streaming 강제 별칭")
async def chat_stream(
    request: ChatCompletionRequest,
    http_request: Request,
    service: ChatService = Depends(get_chat_service),
    ctx: RequestContext = Depends(get_request_ctx),
) -> StreamingResponse:
    """TODO: request.stream = True 로 강제한 뒤 chat_completions 와 동일 경로."""
    raise NotImplementedError


async def _sse(chunks: AsyncIterator[object]) -> AsyncIterator[str]:
    """chunk iterator -> SSE 바이트 스트림.

    TODO: 구현.
      async for chunk in chunks:
          yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
      yield SSE_DONE

    주의:
      - 각 이벤트는 반드시 빈 줄(\n\n)로 끝난다. 하나라도 빠지면 클라이언트가 멈춘다.
      - 스트림 도중 에러가 나면 에러 chunk 를 보내고 [DONE] 으로 닫는다.
        HTTP 상태는 이미 200 이라 바꿀 수 없다 (docs/specs/api-spec.md).
      - asyncio.CancelledError 는 삼키지 말고 정리 후 재전파한다.
    """
    raise NotImplementedError
    yield  # pragma: no cover  (async generator 표식)


def _gateway_headers(ctx: RequestContext) -> dict[str, str]:
    """TODO: X-Gateway-Deployment 등 확장 헤더를 만든다.

    Phase 6 에서 X-Gateway-Fallback, Phase 7 에서 X-Gateway-Experiment/Variant 가 추가된다.
    """
    raise NotImplementedError
