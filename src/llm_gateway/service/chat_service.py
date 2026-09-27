"""Chat 요청 오케스트레이션.

파이프라인 (docs/00-architecture.md "4. 요청 처리 파이프라인"):

    (1) HTTP 수신          -> route
    (2) RequestContext     -> middleware
    (3) Registry 조회       candidates
    (4) Router 선택         ModelRouter (Static / Weighted / HealthAware)
    (5) Resilience 래핑     ResiliencePolicy (retry/fallback/breaker)
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
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from typing import Any

from ..adapters.base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
    AdapterMessage,
    AdapterTimings,
    AdapterUsage,
)
from ..adapters.factory import AdapterFactory
from ..core.context import RequestContext, sanitize_header_value
from ..core.errors import (
    GatewayError,
    InternalError,
    InvalidRequestError,
    RequestCancelledError,
    UnsupportedParameterError,
)
from ..core.logging import log_event
from ..core.timing import ChatTimings, Stopwatch
from ..observability import metrics
from ..registry.models import ModelDeployment, ModelRegistry
from ..resilience.breaker import CircuitBreakers
from ..resilience.policy import ResiliencePolicy
from ..routing.decision import RoutingContext, RoutingDecision
from ..routing.health import HealthTracker
from ..routing.router import ModelRouter
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
        self._breakers = CircuitBreakers()
        self._health = HealthTracker(registry)
        self._router = ModelRouter(registry, self._breakers, self._health)
        self._policy = ResiliencePolicy(
            registry, self._breakers, observe=self._health.observe, available=self._health.available
        )

    # ── 공개 API ────────────────────────────────────────────────

    def routing_status(self) -> dict[str, Any]:
        return self._health.report()

    def prepare(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        headers: Mapping[str, str] | None = None,
    ) -> ModelDeployment:
        """검증 및 최초 후보 선택. 실제 응답 배포는 실행 중 fallback으로 달라질 수 있다.

        Streaming 경로는 PreparedStream에서 첫 content/final을 기다린 뒤 헤더를 확정한다.
        """
        try:
            self._validate(request)
            return self._select(request, ctx, headers)
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
        deployment = deployment or self.prepare(request, ctx)
        sw = Stopwatch().start()
        parts: list[str] = []
        usage = AdapterUsage()
        upstream_timings = AdapterTimings()
        finish_reason = "stop"
        try:
            async with aclosing(self._resilient_chunks(request, ctx, deployment)) as chunks:
                async for chunk in chunks:
                    if chunk.delta:
                        sw.mark_first_token()
                        parts.append(chunk.delta)
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if chunk.timings is not None:
                        upstream_timings = chunk.timings
                    if chunk.finish_reason is not None:
                        finish_reason = chunk.finish_reason
        except asyncio.CancelledError:
            self._on_cancelled(request, ctx.current_deployment or deployment, ctx, sw, stream=False)
            raise
        except Exception as exc:
            self._on_failed(request, ctx.current_deployment or deployment, ctx, exc, stream=False)
            raise
        actual = ctx.current_deployment or deployment
        response = AdapterChatResponse(
            content="".join(parts),
            finish_reason=finish_reason,
            usage=usage,
            timings=upstream_timings,
            upstream_model=actual.upstream_model,
        )
        timings = self._merge_timings(sw.finish(), upstream_timings)
        self._on_completed(request, actual, response, timings, stream=False)
        return ChatResult(
            self._to_openai_response(response, request, ctx), actual, timings, usage.source
        )

    async def stream(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        deployment: ModelDeployment | None = None,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        deployment = deployment or self.prepare(request, ctx)
        completion_id = _completion_id(ctx)
        created = int(time.time())
        sw = Stopwatch().start()
        usage = AdapterUsage()
        upstream_timings = AdapterTimings()
        finish_reason = "stop"
        role_sent = False
        try:
            async with aclosing(self._resilient_chunks(request, ctx, deployment)) as chunks:
                async for chunk in chunks:
                    if chunk.delta:
                        sw.mark_first_token()
                    # Policy filters empty keepalives. The actual destination is now committed.
                    if not role_sent:
                        role_sent = True
                        yield ChatCompletionChunk(
                            id=completion_id,
                            created=created,
                            model=request.model,
                            choices=[
                                ChatCompletionChunkChoice(
                                    delta=ChatCompletionDelta(role="assistant"),
                                )
                            ],
                        )
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if chunk.timings is not None:
                        upstream_timings = chunk.timings
                    if chunk.finish_reason is not None:
                        finish_reason = chunk.finish_reason
                    yield self._to_openai_chunk(chunk, request, completion_id, created)
        except (asyncio.CancelledError, GeneratorExit):
            self._on_cancelled(request, ctx.current_deployment or deployment, ctx, sw, stream=True)
            raise
        except Exception as exc:
            self._on_failed(request, ctx.current_deployment or deployment, ctx, exc, stream=True)
            raise
        actual = ctx.current_deployment or deployment
        self._on_completed(
            request,
            actual,
            AdapterChatResponse(
                content="",
                finish_reason=finish_reason,
                usage=usage,
                timings=upstream_timings,
                upstream_model=actual.upstream_model,
            ),
            self._merge_timings(sw.finish(), upstream_timings),
            stream=True,
        )

    async def _resilient_chunks(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        deployment: ModelDeployment,
    ) -> AsyncGenerator[AdapterChatChunk, None]:
        decision = ctx.routing_decision or RoutingDecision(deployment, "provided", "static")
        config = ctx.resilience_config or self._registry.snapshot.resilience
        async with aclosing(
            self._policy.execute(
                decision,
                config,
                ctx,
                lambda candidate: self._call(request, candidate),
            )
        ) as chunks:
            async for chunk in chunks:
                yield chunk

    async def _call(
        self,
        request: ChatCompletionRequest,
        deployment: ModelDeployment,
    ) -> AsyncGenerator[AdapterChatChunk, None]:
        adapter_request = self._build_adapter_request(request, deployment)
        with (
            self._adapters.lease(deployment) as adapter,
            metrics.inflight_tracker(
                request.model,
                deployment.id,
            ),
        ):
            chunks = adapter.stream_chat(adapter_request)
            try:
                async for chunk in chunks:
                    yield chunk
            finally:
                close = getattr(chunks, "aclose", None)
                if close is not None:
                    await close()

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
        _output_format(request)  # json_schema 형식 오류를 adapter 호출 전에 GW-4000 으로

    def _select(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        headers: Mapping[str, str] | None = None,
    ) -> ModelDeployment:
        snapshot = self._registry.snapshot
        header_name = snapshot.routing.bucket_header.lower()
        if headers is not None:
            hints = {key.lower(): value for key, value in headers.items()}
            primary = sanitize_header_value(hints.get(header_name))
            user_bucket = sanitize_header_value(hints.get("x-user-bucket"))
        else:
            primary = (
                sanitize_header_value(ctx.session_id) if header_name == "x-session-id" else None
            )
            user_bucket = sanitize_header_value(ctx.user_bucket)
        bucket_key = primary or user_bucket or ctx.request_id
        source = "primary_header" if primary else "user_bucket" if user_bucket else "request_id"
        decision = self._router.route(
            RoutingContext(
                model=request.model,
                bucket_key=bucket_key,
                bucket_source=source,
                request_type=ctx.request_type,
                estimated_input_tokens=(sum(len(m.content) for m in request.messages) + 3) // 4,
                max_tokens=request.max_tokens,
            ),
            snapshot=snapshot,
        )
        deployment = decision.deployment
        ctx.routing_decision = decision
        ctx.resilience_config = snapshot.resilience
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
                "num_ctx": request.num_ctx,
            }
        )
        json_output, json_schema = _output_format(request)

        return AdapterChatRequest(
            # 논리명이 아니라 upstream 모델명을 넣는다.
            model=deployment.upstream_model,
            messages=[AdapterMessage(role=m.role, content=m.content) for m in request.messages],
            temperature=options.temperature,
            top_p=options.top_p,
            max_tokens=options.max_tokens,
            stop=options.stop,
            seed=options.seed,
            num_ctx=options.num_ctx,
            json_output=json_output,
            json_schema=json_schema,
            extra=dict(deployment.extra),
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


def _output_format(request: ChatCompletionRequest) -> tuple[bool, dict[str, Any] | None]:
    """OpenAI response_format -> (json_output, json_schema).

    {"type": "json_schema", "json_schema": {"name": ..., "schema": {...}}} 의 schema 만 꺼낸다.
    """
    fmt = request.response_format
    if fmt is None or fmt.get("type") == "text":
        return False, None
    if fmt.get("type") == "json_object":
        return True, None

    spec = fmt.get("json_schema")
    schema = spec.get("schema") if isinstance(spec, dict) else None
    if not isinstance(schema, dict):
        raise InvalidRequestError(
            "response_format.json_schema.schema must be an object",
            detail={"fields": ["response_format.json_schema.schema"]},
        )
    return True, schema


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
