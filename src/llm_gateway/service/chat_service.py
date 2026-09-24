"""Chat 요청 오케스트레이션.

파이프라인 (docs/00-architecture.md "4. 요청 처리 파이프라인"):

    (1) HTTP 수신          -> route
    (2) RequestContext     -> middleware
    (3) Registry 조회       resolve/candidates
    (4) Router 선택         [Phase 5]  <- 지금은 registry.resolve() 가 대신한다
    (5) Resilience 래핑     [Phase 6]  <- 지금은 직접 호출
    (6) Adapter 호출        여기
    (7) 응답 정규화          여기
    (8) 계측 기록           여기 (observability.metrics)

**이 파일의 구조가 이후 모든 Phase를 좌우한다.**
(4)(5)(8) 이 나중에 끼어들 자리를 지금 비워두는 것이 Phase 1 의 핵심 작업이다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from ..adapters.base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
    AdapterMessage,
    AdapterTimings,
    AdapterUsage,
    LLMAdapter,
)
from ..adapters.factory import AdapterFactory
from ..core.context import RequestContext
from ..core.errors import (
    GatewayError,
    InternalError,
    RequestCancelledError,
    UnsupportedParameterError,
)
from ..core.logging import log_event
from ..core.timing import ChatTimings, Stopwatch
from ..observability import metrics
from ..registry.models import ModelDeployment, ModelRegistry
from ..schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
)
from ..schemas.common import Usage

log = logging.getLogger(__name__)


class ChatResult:
    """서비스 계층의 반환값 - 응답 + 계측 자료를 함께 들고 나온다.

    라우터가 응답 헤더(X-Gateway-Deployment 등)를 채우고,
    Phase 2 가 metric 을 기록하는 데 쓰인다.
    """

    def __init__(
        self,
        response: ChatCompletionResponse,
        deployment: ModelDeployment,
        timings: ChatTimings,
        usage_source: str,
    ) -> None:
        self.response = response
        self.deployment = deployment
        self.timings = timings
        self.usage_source = usage_source


class ChatService:
    def __init__(self, registry: ModelRegistry, adapters: AdapterFactory) -> None:
        self._registry = registry
        self._adapters = adapters

    # ── 공개 API ────────────────────────────────────────────────

    def prepare(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> ModelDeployment:
        """검증 + deployment 선택. **본문을 흘리기 전에** 끝나야 하는 일이다.

        streaming 응답의 헤더(X-Gateway-Deployment)는 첫 바이트 전에 확정되어야 하는데,
        async generator 는 첫 __anext__ 까지 아무것도 실행하지 않는다.
        그래서 선택을 이 동기 메서드로 떼어내 라우터가 먼저 호출한다.
        """
        try:
            self._validate(request)
            return self._select(request, ctx)
        except GatewayError as exc:
            # deployment 가 없으므로 requests_total 은 올리지 않는다. 에러 카운터만.
            self._record_error(exc, ctx, model=self._model_label(request.model), deployment=None)
            raise

    async def complete(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        deployment: ModelDeployment | None = None,
    ) -> ChatResult:
        """Non-streaming 응답을 만든다.

        내부적으로는 streaming 호출을 돌려 chunk 를 합친다.
        non-streaming 요청에서도 TTFT 를 얻기 위한 구조다
        (docs/00-architecture.md "7. 개발 규약" 6번).
        """
        deployment = deployment or self.prepare(request, ctx)
        adapter = self._adapters.get(deployment)
        adapter_req = self._build_adapter_request(request, deployment)

        sw = Stopwatch().start()
        with metrics.inflight_tracker(request.model, deployment.id):
            try:
                adapter_response = await self._aggregate_stream(adapter, adapter_req, sw)
            except asyncio.CancelledError:
                self._on_cancelled(request, deployment, ctx, sw, stream=False)
                raise
            except Exception as exc:
                self._on_failed(request, deployment, ctx, exc, stream=False)
                raise
        timings = self._merge_timings(sw.finish(), adapter_response.timings)

        self._on_completed(request, deployment, adapter_response, timings, stream=False)

        return ChatResult(
            response=self._to_openai_response(adapter_response, request, ctx),
            deployment=deployment,
            timings=timings,
            usage_source=adapter_response.usage.source,
        )

    async def stream(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        deployment: ModelDeployment | None = None,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Streaming 응답을 만든다.

        스트림 도중 에러는 여기서 삼키지 않고 올린다. HTTP 상태는 이미 200 이므로
        SSE 인코더(api/routes/chat.py)가 에러 chunk 로 바꿔 내보낸다.
        """
        deployment = deployment or self.prepare(request, ctx)
        adapter = self._adapters.get(deployment)
        adapter_req = self._build_adapter_request(request, deployment)

        completion_id = _completion_id(ctx)
        created = int(time.time())
        sw = Stopwatch().start()

        last_chunk: AdapterChatChunk | None = None
        with metrics.inflight_tracker(request.model, deployment.id):
            try:
                # OpenAI 스트림의 첫 chunk 는 role 만 담는다. 여기에는 content 가 없으므로
                # TTFT 로 세지 않는다.
                yield ChatCompletionChunk(
                    id=completion_id,
                    created=created,
                    model=request.model,
                    choices=[
                        ChatCompletionChunkChoice(delta=ChatCompletionDelta(role="assistant"))
                    ],
                )

                async for chunk in adapter.stream_chat(adapter_req):
                    if chunk.delta:
                        # 비어있지 않은 첫 delta 만 TTFT 기준이다.
                        sw.mark_first_token()
                        # Phase 6 의 재시도/폴백 차단 플래그. 한 글자라도 나갔으면 되돌릴 수 없다.
                        ctx.stream_started = True
                    last_chunk = chunk
                    yield self._to_openai_chunk(chunk, request, completion_id, created)
            except (asyncio.CancelledError, GeneratorExit):
                # 클라이언트가 yield 대기 중에 끊으면 CancelledError 가 아니라
                # 제너레이터 aclose() 의 GeneratorExit 로 들어온다. 둘 다 이탈이다.
                self._on_cancelled(request, deployment, ctx, sw, stream=True)
                raise
            except Exception as exc:
                self._on_failed(request, deployment, ctx, exc, stream=True)
                raise

        timings = self._merge_timings(
            sw.finish(), last_chunk.timings if last_chunk else None
        )
        usage = last_chunk.usage if last_chunk else None

        self._on_completed(
            request,
            deployment,
            AdapterChatResponse(
                content="",
                finish_reason=(last_chunk.finish_reason if last_chunk else "error") or "stop",
                usage=usage or AdapterUsage(),
                timings=(last_chunk.timings if last_chunk else None) or AdapterTimings(),
                upstream_model=deployment.upstream_model,
            ),
            timings,
            stream=True,
        )

    # ── 내부 ────────────────────────────────────────────────────

    def _validate(self, request: ChatCompletionRequest) -> None:
        """미지원 파라미터를 조용히 무시하지 않고 거절한다 (GW-4002).

        Spring 쪽에서 tools 를 보내기 시작했는데 Gateway 가 버리고 있으면
        원인 파악에 오래 걸린다.
        """
        unsupported = request.unsupported_fields()
        if unsupported:
            raise UnsupportedParameterError(
                f"unsupported parameter(s): {', '.join(unsupported)}",
                detail={"fields": unsupported},
            )

    def _select(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> ModelDeployment:
        """논리 모델 -> deployment.

        Phase 5 에서 이 메서드 내부만 ModelRouter 호출로 교체된다.
        **호출부는 바뀌지 않아야 한다.** 그래서 별도 메서드로 분리해둔다.
        """
        deployment = self._registry.resolve(request.model)
        ctx.model = request.model
        ctx.deployment_id = deployment.id
        return deployment

    def _build_adapter_request(
        self, request: ChatCompletionRequest, deployment: ModelDeployment
    ) -> AdapterChatRequest:
        """OpenAI 스키마 -> 중립 DTO. 파라미터 병합도 여기서 한다.

        병합 우선순위: 요청 파라미터 > deployment.options > defaults.options
        (defaults 는 로딩 시점에 이미 deployment.options 에 병합되어 있다)

        요청에서 생략된 값은 None 으로 들어온다. merged_with() 가 None 을 "미지정"으로
        처리하므로 deployment 기본값이 지워지지 않는다.
        """
        options = deployment.options.merged_with(
            {
                "temperature": request.temperature,
                "top_p": request.top_p,
                "max_tokens": request.max_tokens,
                "stop": request.stop,
                "seed": request.seed,
            }
        )

        return AdapterChatRequest(
            # 논리명이 아니라 upstream 모델명을 넣는다.
            model=deployment.upstream_model,
            messages=[AdapterMessage(role=m.role, content=m.content) for m in request.messages],
            temperature=options.temperature,
            top_p=options.top_p,
            max_tokens=options.max_tokens,
            stop=options.stop,
            seed=options.seed,
            extra=dict(deployment.extra),
        )

    async def _aggregate_stream(
        self,
        adapter: LLMAdapter,
        adapter_req: AdapterChatRequest,
        stopwatch: Stopwatch,
    ) -> AdapterChatResponse:
        """streaming 호출을 돌려 하나의 응답으로 합친다.

        non-streaming 요청에서도 TTFT 를 얻기 위한 장치다.
        (docs/phases/phase-02-instrumentation.md "3.1 TTFT")
        """
        parts: list[str] = []
        finish_reason = "stop"
        usage = AdapterUsage()
        timings = AdapterTimings()

        async for chunk in adapter.stream_chat(adapter_req):
            if chunk.delta:
                stopwatch.mark_first_token()
                parts.append(chunk.delta)
            # 마지막 chunk 에만 실려 오는 값들. 덮어쓰지 말고 있을 때만 보존한다.
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.timings is not None:
                timings = chunk.timings

        return AdapterChatResponse(
            content="".join(parts),
            finish_reason=finish_reason,
            usage=usage,
            timings=timings,
            upstream_model=adapter_req.model,
        )

    def _to_openai_response(
        self,
        adapter_response: AdapterChatResponse,
        request: ChatCompletionRequest,
        ctx: RequestContext,
    ) -> ChatCompletionResponse:
        """중립 DTO -> OpenAI 응답.

        Spring AI 가 기대하는 필드를 하나라도 빠뜨리면 파싱에 실패한다.
        """
        return ChatCompletionResponse(
            id=_completion_id(ctx),
            created=int(time.time()),
            # 요청한 **논리 모델명**을 돌려준다. 실제 deployment 는 헤더로 알린다.
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=adapter_response.content),
                    finish_reason=adapter_response.finish_reason,
                )
            ],
            usage=_to_usage(adapter_response.usage),
        )

    def _to_openai_chunk(
        self,
        adapter_chunk: AdapterChatChunk,
        request: ChatCompletionRequest,
        completion_id: str,
        created: int,
    ) -> ChatCompletionChunk:
        """중립 chunk -> OpenAI chunk."""
        return ChatCompletionChunk(
            id=completion_id,
            created=created,
            model=request.model,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=ChatCompletionDelta(content=adapter_chunk.delta or None),
                    finish_reason=adapter_chunk.finish_reason,
                )
            ],
            usage=_to_usage(adapter_chunk.usage) if adapter_chunk.usage else None,
        )

    @staticmethod
    def _merge_timings(measured: ChatTimings, upstream: AdapterTimings | None) -> ChatTimings:
        """Gateway 가 잰 값 + upstream 이 준 원자료.

        total/ttft 는 Gateway 관점(네트워크 포함)이 정답이므로 그대로 둔다.
        upstream 값은 Gateway 가 알 수 없는 구간(prefill/load/queue)만 채운다.
        """
        if upstream is None:
            return measured
        return ChatTimings(
            total_sec=measured.total_sec,
            ttft_sec=measured.ttft_sec,
            generation_sec=measured.generation_sec,
            queue_sec=upstream.queue_sec,
            prompt_eval_sec=upstream.prompt_eval_sec,
            load_sec=upstream.load_sec,
        )

    # ── 계측 (Phase 2) ──────────────────────────────────────────
    # 요청 하나당 아래 셋 중 정확히 하나가 불린다.

    def _on_completed(
        self,
        request: ChatCompletionRequest,
        deployment: ModelDeployment,
        adapter_response: AdapterChatResponse,
        timings: ChatTimings,
        *,
        stream: bool,
    ) -> None:
        """요청당 LLM 요약 로그 1줄 + metric. 프롬프트/응답 원문은 넣지 않는다."""
        usage = adapter_response.usage
        log_event(
            log,
            "chat_completed",
            model=request.model,
            deployment_id=deployment.id,
            adapter=deployment.adapter,
            upstream_model=deployment.upstream_model,
            stream=stream,
            status=metrics.STATUS_SUCCESS,
            finish_reason=adapter_response.finish_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            token_source=usage.source,
            total_sec=_round(timings.total_sec),
            ttft_sec=_round(timings.ttft_sec),
            generation_sec=_round(timings.generation_sec),
            queue_sec=_round(timings.queue_sec),
            prompt_eval_sec=_round(timings.prompt_eval_sec),
            load_sec=_round(timings.load_sec),
            output_tps=_round(timings.output_tps(usage.output_tokens)),
        )
        metrics.record_request(
            model=request.model,
            deployment_id=deployment.id,
            adapter=deployment.adapter,
            stream=stream,
            status=metrics.STATUS_SUCCESS,
            timings=timings,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            token_source=usage.source,
            finish_reason=adapter_response.finish_reason,
        )

    def _on_failed(
        self,
        request: ChatCompletionRequest,
        deployment: ModelDeployment,
        ctx: RequestContext,
        exc: Exception,
        *,
        stream: bool,
    ) -> None:
        """adapter 호출 실패. 로그는 에러 핸들러/SSE 인코더가 남기므로 metric 만 기록한다."""
        error = exc if isinstance(exc, GatewayError) else InternalError("internal error")
        self._record_error(error, ctx, model=request.model, deployment=deployment)
        metrics.record_request(
            model=request.model,
            deployment_id=deployment.id,
            adapter=deployment.adapter,
            stream=stream,
            status=metrics.STATUS_ERROR,
            timings=None,
            input_tokens=None,
            output_tokens=None,
            token_source="",
            finish_reason="error",
        )

    def _on_cancelled(
        self,
        request: ChatCompletionRequest,
        deployment: ModelDeployment,
        ctx: RequestContext,
        stopwatch: Stopwatch,
        *,
        stream: bool,
    ) -> None:
        """클라이언트 이탈. 실패가 아니라 별도 사유로 센다 (GW-4007).

        error rate 에 섞이지 않도록 status="cancelled" 로 기록한다.
        """
        cancelled = RequestCancelledError("client closed the connection")
        log_event(
            log,
            "chat_cancelled",
            level=logging.WARNING,
            model=request.model,
            deployment_id=deployment.id,
            stream=stream,
            status=metrics.STATUS_CANCELLED,
            code=cancelled.code,
            error_type=str(cancelled.error_type),
            elapsed_sec=_round(stopwatch.elapsed_sec),
        )
        self._record_error(cancelled, ctx, model=request.model, deployment=deployment)
        metrics.record_request(
            model=request.model,
            deployment_id=deployment.id,
            adapter=deployment.adapter,
            stream=stream,
            status=metrics.STATUS_CANCELLED,
            timings=None,
            input_tokens=None,
            output_tokens=None,
            token_source="",
            finish_reason="cancelled",
        )

    @staticmethod
    def _record_error(
        error: GatewayError,
        ctx: RequestContext,
        *,
        model: str | None,
        deployment: ModelDeployment | None,
    ) -> None:
        """errors_total 증가 + 기록했다는 표시.

        표시가 없으면 main.gateway_error_handler 가 같은 에러를 한 번 더 센다.
        """
        metrics.record_error(
            model=model,
            deployment_id=deployment.id if deployment else None,
            error_type=str(error.error_type),
            code=error.code,
        )
        ctx.error_recorded = True

    def _model_label(self, requested: str) -> str | None:
        """클라이언트가 보낸 모델명은 등록된 것일 때만 label 로 쓴다 (자유 문자열 금지)."""
        return requested if requested in self._registry.snapshot.models else None


def _completion_id(ctx: RequestContext) -> str:
    """OpenAI 규약의 id. request_id 와 이어져야 로그 추적이 쉬워진다."""
    return f"chatcmpl-{ctx.request_id[:8]}"


def _to_usage(usage: AdapterUsage | None) -> Usage | None:
    """중립 usage -> OpenAI usage. 모르는 값은 0 이 아니라 None 으로 둔다."""
    if usage is None:
        return None
    total = (
        None
        if usage.input_tokens is None or usage.output_tokens is None
        else usage.input_tokens + usage.output_tokens
    )
    return Usage(
        prompt_tokens=usage.input_tokens,
        completion_tokens=usage.output_tokens,
        total_tokens=total,
    )


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)
