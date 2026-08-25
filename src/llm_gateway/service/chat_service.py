"""Chat 요청 오케스트레이션.

파이프라인 (docs/00-architecture.md "4. 요청 처리 파이프라인"):

    (1) HTTP 수신          -> route
    (2) RequestContext     -> middleware
    (3) Registry 조회       resolve/candidates
    (4) Router 선택         [Phase 5]  <- 지금은 registry.resolve() 가 대신한다
    (5) Resilience 래핑     [Phase 6]  <- 지금은 직접 호출
    (6) Adapter 호출        여기
    (7) 응답 정규화          여기
    (8) 계측 기록           [Phase 2]  <- 자리만 잡아둔다

**이 파일의 구조가 이후 모든 Phase를 좌우한다.**
(4)(5)(8) 이 나중에 끼어들 자리를 지금 비워두는 것이 Phase 1 의 핵심 작업이다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..adapters.base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
)
from ..adapters.factory import AdapterFactory
from ..core.context import RequestContext
from ..core.timing import ChatTimings, Stopwatch
from ..registry.models import ModelDeployment, ModelRegistry
from ..schemas.chat import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)


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

    async def complete(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> ChatResult:
        """Non-streaming 응답을 만든다.

        TODO: 구현.
          1. _validate(request)
          2. deployment = self._select(request, ctx)
          3. adapter_req = self._build_adapter_request(request, deployment)
          4. sw = Stopwatch().start()
          5. 응답 생성:
             (권장) stream_chat 을 돌려 chunk 를 합친다 -> TTFT 확보
                    content, last_chunk = await self._aggregate_stream(adapter, adapter_req, sw)
             (단순) await adapter.chat(adapter_req)     -> TTFT 는 None 이 된다
          6. timings = sw.finish()  (+ adapter 가 준 timings 병합)
          7. _to_openai_response(...)

        Phase 2 에서 6번 뒤에 metric 기록 한 줄이 들어간다.
        Phase 5 에서 2번이 router.select() 로 바뀐다.
        Phase 6 에서 5번이 resilience.execute(...) 로 감싸진다.
        """
        raise NotImplementedError

    async def stream(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Streaming 응답을 만든다.

        TODO: 구현.
          - adapter.stream_chat() 을 돌며 AdapterChatChunk -> ChatCompletionChunk 변환
          - **delta 가 비어있지 않은 첫 chunk** 에서 sw.mark_first_token() 과
            ctx.stream_started = True 를 함께 세운다.
            (stream_started 는 Phase 6 에서 재시도/폴백을 차단하는 플래그다)
          - 마지막 chunk 에 finish_reason 과 usage 를 실어 보낸다
          - 완료 후 sw.finish() -> 계측 (Phase 2)

        예외 처리:
          - 스트림 도중 에러: HTTP 상태는 이미 200 이라 바꿀 수 없다.
            에러 chunk 를 하나 보내고 정상 종료한다 (docs/specs/api-spec.md 참고).
          - asyncio.CancelledError: 클라이언트 이탈. RequestCancelledError 로 기록하고
            upstream 스트림을 반드시 닫는다.
        """
        raise NotImplementedError
        yield  # pragma: no cover  (async generator 표식)

    # ── 내부 ────────────────────────────────────────────────────

    def _validate(self, request: ChatCompletionRequest) -> None:
        """TODO: 미지원 파라미터 검사 -> UnsupportedParameterError (GW-4002).

        조용히 무시하지 않는 것이 중요하다. Spring 쪽에서 tools 를 보내기 시작했는데
        Gateway 가 버리고 있으면 원인 파악에 오래 걸린다.
        """
        raise NotImplementedError

    def _select(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> ModelDeployment:
        """논리 모델 -> deployment.

        TODO: Phase 1 에서는 self._registry.resolve(request.model).
              선택 결과를 ctx.deployment_id 에 기록할 것 (로그 상관용).

        Phase 5 에서 이 메서드 내부만 ModelRouter 호출로 교체된다.
        **호출부는 바뀌지 않아야 한다.** 그래서 별도 메서드로 분리해둔다.
        """
        raise NotImplementedError

    def _build_adapter_request(
        self, request: ChatCompletionRequest, deployment: ModelDeployment
    ) -> AdapterChatRequest:
        """OpenAI 스키마 -> 중립 DTO. 파라미터 병합도 여기서 한다.

        TODO: 구현. 병합 우선순위:
            요청 파라미터 > deployment.options > defaults.options

        주의: 요청에서 생략된 값은 None 으로 들어온다. 이걸 그대로 덮어쓰면
              deployment 기본값이 지워진다. **None 은 "미지정"으로 처리할 것.**

        model 필드에는 논리명이 아니라 deployment.upstream_model 을 넣는다.
        """
        raise NotImplementedError

    async def _aggregate_stream(
        self,
        adapter_req: AdapterChatRequest,
        deployment: ModelDeployment,
        stopwatch: Stopwatch,
    ) -> AdapterChatResponse:
        """streaming 호출을 돌려 하나의 응답으로 합친다.

        non-streaming 요청에서도 TTFT 를 얻기 위한 장치다.
        (docs/phases/phase-02-instrumentation.md "3.1 TTFT")

        TODO: 구현.
          - chunk.delta 를 이어붙인다
          - 비어있지 않은 첫 delta 에서 stopwatch.mark_first_token()
          - 마지막 chunk 의 usage/timings/finish_reason 을 보존한다
        """
        raise NotImplementedError

    def _to_openai_response(
        self,
        adapter_response: AdapterChatResponse,
        request: ChatCompletionRequest,
        ctx: RequestContext,
    ) -> ChatCompletionResponse:
        """중립 DTO -> OpenAI 응답.

        TODO: 구현. 주의사항:
          - id 는 "chatcmpl-" + request_id 앞 8자 정도로 만든다
          - created 는 epoch seconds (int)
          - model 에는 **요청한 논리 모델명**을 넣는다 (upstream 모델명이 아니다).
            논리/물리 분리를 응답 body 에서 깨뜨리지 않기 위함.
            실제 deployment 는 X-Gateway-Deployment 헤더로 알린다.
          - usage 는 None 일 수 있다. 0 으로 채우지 말 것.
          - Spring AI 가 기대하는 필드를 하나라도 빠뜨리면 파싱에 실패한다.
        """
        raise NotImplementedError

    def _to_openai_chunk(
        self,
        adapter_chunk: AdapterChatChunk,
        request: ChatCompletionRequest,
        completion_id: str,
        created: int,
    ) -> ChatCompletionChunk:
        """TODO: 중립 chunk -> OpenAI chunk."""
        raise NotImplementedError
