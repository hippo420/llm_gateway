"""Ollama Adapter.

Endpoint: POST {endpoint}/api/chat
매핑 표: docs/specs/adapter-interface.md "4. Ollama 매핑"

주의사항 (구현 중 가장 자주 틀리는 것들):
  - Ollama 의 duration 필드는 **나노초**다. core.timing.ns_to_sec 를 쓸 것.
  - streaming 응답은 SSE 가 아니라 **NDJSON** (줄바꿈 구분 JSON) 이다.
  - prompt_eval_count / eval_count 가 없으면 None (0 아님).
  - load_duration 이 크면 cold start. keep_alive 설정을 의심할 것.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..registry.models import ModelDeployment
from .base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
    AdapterTimings,
    AdapterUsage,
    LLMAdapter,
)


class OllamaAdapter(LLMAdapter):
    name = "ollama"

    def __init__(self, deployment: ModelDeployment) -> None:
        super().__init__(deployment)
        # TODO: httpx.AsyncClient 를 여기서 한 번만 만들고 재사용한다.
        #       요청마다 새로 만들면 connection pool 이 무의미해지고 TTFT 가 나빠진다.
        #       timeout 은 _build_timeout() 참고.
        self._client: httpx.AsyncClient | None = None

    # ── 공개 API ────────────────────────────────────────────────

    async def chat(self, request: AdapterChatRequest) -> AdapterChatResponse:
        """Non-streaming 호출 (stream=false).

        TODO: 구현.
          1. _build_payload(request, stream=False)
          2. POST {endpoint}/api/chat
          3. _parse_final(json) -> AdapterChatResponse

        참고: TTFT 를 재려면 streaming 이 필요하다. ChatService 가 내부적으로
              stream_chat 을 쓰고 합치는 방식을 택하면 이 메서드는 거의 안 쓰인다.
              (docs/phases/phase-02-instrumentation.md "3.1 TTFT")
        """
        raise NotImplementedError

    async def stream_chat(  # type: ignore[override]
        self, request: AdapterChatRequest
    ) -> AsyncIterator[AdapterChatChunk]:
        """Streaming 호출 (stream=true, NDJSON).

        TODO: 구현.
          async with client.stream("POST", "/api/chat", json=payload) as resp:
              _raise_for_status(resp)
              async for line in resp.aiter_lines():
                  if not line.strip():
                      continue
                  data = json.loads(line)
                  if data.get("done"):
                      yield 마지막 chunk (finish_reason + usage + timings)
                      break
                  yield AdapterChatChunk(delta=data["message"]["content"])

        주의:
          - 마지막 done=true 응답에만 토큰/타이밍이 들어 있다. 놓치면 지표가 통째로 빈다.
          - asyncio.CancelledError 를 잡아 upstream 응답을 반드시 close 할 것.
            (클라이언트가 끊었는데 GPU 는 계속 생성하는 상황을 막는다)
          - 여기서 재시도하지 않는다.
        """
        raise NotImplementedError
        yield  # pragma: no cover  (async generator 로 만들기 위한 표식)

    async def health(self) -> bool:
        """TODO: GET {endpoint}/api/tags 로 도달 가능 여부 확인. 짧은 timeout 을 쓸 것."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """TODO: self._client.aclose()"""
        raise NotImplementedError

    # ── 내부 ────────────────────────────────────────────────────

    def _build_payload(self, request: AdapterChatRequest, *, stream: bool) -> dict[str, Any]:
        """중립 DTO -> Ollama 요청 body.

        TODO: 구현. 매핑:
            max_tokens  -> options.num_predict
            temperature -> options.temperature
            top_p       -> options.top_p
            stop        -> options.stop
            seed        -> options.seed
            extra.keep_alive -> keep_alive   (cold start 방지)

        None 인 값은 options 에 넣지 않는다 (Ollama 가 null 을 싫어한다).
        """
        raise NotImplementedError

    def _parse_final(self, data: dict[str, Any]) -> AdapterChatResponse:
        """done=true 응답 -> AdapterChatResponse.

        TODO: 구현. _extract_usage / _extract_timings 를 사용.
        """
        raise NotImplementedError

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> AdapterUsage:
        """TODO: prompt_eval_count -> input, eval_count -> output.

        키가 없으면 None. `data.get("eval_count", 0)` 처럼 0 을 기본값으로 쓰지 말 것.
        """
        raise NotImplementedError

    @staticmethod
    def _extract_timings(data: dict[str, Any]) -> AdapterTimings:
        """TODO: 나노초 필드를 초로 변환 (ns_to_sec).

            prompt_eval_duration -> prompt_eval_sec
            eval_duration        -> generation_sec
            load_duration        -> load_sec

        Ollama 는 queue 시간을 주지 않는다. queue_sec 는 None 으로 둔다.
        (추정치로 채우면 진단 규칙 R1/R2 가 틀린 판단을 한다)
        """
        raise NotImplementedError

    def _build_timeout(self) -> httpx.Timeout:
        """TODO: deployment.timeout -> httpx.Timeout 변환.

            connect -> connect
            read    -> read    (streaming 에서는 chunk 간 간격에 걸린다)
            total   -> pool/write 및 상위 total 가드

        httpx 의 read timeout 은 이미 "chunk 간" 이라 streaming 과 궁합이 맞는다.
        total 은 httpx 가 직접 지원하지 않으므로 asyncio.timeout 으로 감싸야 한다.
        """
        raise NotImplementedError

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """upstream HTTP 에러 -> GatewayError 변환.

        TODO: 구현. 매핑 힌트:
            404 + "model not found"   -> ModelNotFoundError / ModelLoadingError
            메시지에 "out of memory"   -> OutOfMemoryError_  (재시도 금지)
            5xx                       -> UpstreamError (retryable 판단은 상위)
            그 외                     -> UpstreamError

        httpx 예외는 호출부에서 변환한다:
            ConnectError   -> UpstreamUnavailableError
            ConnectTimeout -> UpstreamConnectTimeoutError
            ReadTimeout    -> UpstreamReadTimeoutError
            JSONDecodeError-> UpstreamProtocolError
        """
        raise NotImplementedError
