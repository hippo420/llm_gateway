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

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.errors import (
    GatewayError,
    ModelLoadingError,
    ModelNotFoundError,
    OutOfMemoryError_,
    UpstreamConnectTimeoutError,
    UpstreamError,
    UpstreamProtocolError,
    UpstreamReadTimeoutError,
    UpstreamTotalTimeoutError,
    UpstreamUnavailableError,
)
from ..core.timing import ns_to_sec
from ..registry.models import ModelDeployment
from ..resilience.timeout import http_timeout
from .base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
    AdapterEmbeddingRequest,
    AdapterEmbeddingResponse,
    AdapterTimings,
    AdapterUsage,
    LLMAdapter,
    parse_embedding_vectors,
)

CHAT_PATH = "/api/chat"
EMBEDDINGS_PATH = "/api/embed"
TAGS_PATH = "/api/tags"

# health() 는 readyz 안에서 여러 개가 동시에 돌기 때문에 짧게 끊는다.
HEALTH_TIMEOUT_SEC = 2.0


class OllamaAdapter(LLMAdapter):
    name = "ollama"

    def __init__(self, deployment: ModelDeployment) -> None:
        super().__init__(deployment)
        # client 는 lazy 생성 후 재사용한다. 요청마다 새로 만들면 connection pool 이
        # 무의미해지고 TTFT 가 나빠진다. 생성 시점을 첫 호출로 미루는 이유는
        # AsyncClient 가 생성 시점의 event loop 에 묶이기 때문이다.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    # ── 공개 API ────────────────────────────────────────────────

    async def chat(self, request: AdapterChatRequest) -> AdapterChatResponse:
        """Non-streaming 호출 (stream=false).

        참고: TTFT 를 재려면 streaming 이 필요하다. ChatService 는 내부적으로
              stream_chat 을 써서 합치므로 이 메서드는 TTFT 가 필요 없을 때만 쓰인다.
              (docs/phases/phase-02-instrumentation.md "3.1 TTFT")
        """
        client = await self._ensure_client()
        payload = self._build_payload(request, stream=False)

        try:
            async with asyncio.timeout(self.deployment.timeout.total):
                response = await client.post(CHAT_PATH, json=payload)
                self._raise_for_status(response)
                data = response.json()
        except TimeoutError as exc:
            raise self._translate_exception(exc) from exc
        except httpx.HTTPError as exc:
            raise self._translate_exception(exc) from exc
        except json.JSONDecodeError as exc:
            raise UpstreamProtocolError(
                "upstream returned a non-JSON body",
                detail={"deployment_id": self.deployment.id},
            ) from exc

        return self._parse_final(data)

    async def embed(self, request: AdapterEmbeddingRequest) -> AdapterEmbeddingResponse:
        client = await self._ensure_client()
        payload: dict[str, Any] = {
            "model": request.model,
            "input": request.input,
            "truncate": False,
        }
        if request.dimensions is not None:
            payload["dimensions"] = request.dimensions
        try:
            async with asyncio.timeout(self.deployment.timeout.total):
                response = await client.post(EMBEDDINGS_PATH, json=payload)
                self._raise_for_status(response)
                data = response.json()
        except TimeoutError as exc:
            raise self._translate_exception(exc) from exc
        except httpx.HTTPError as exc:
            raise self._translate_exception(exc) from exc
        except json.JSONDecodeError as exc:
            raise UpstreamProtocolError("upstream returned a non-JSON body") from exc

        prompt_tokens = data.get("prompt_eval_count")
        if prompt_tokens is not None and (type(prompt_tokens) is not int or prompt_tokens < 0):
            raise UpstreamProtocolError("upstream embedding usage is invalid")
        return AdapterEmbeddingResponse(
            embeddings=parse_embedding_vectors(data.get("embeddings"), dimensions=request.dimensions),
            prompt_tokens=prompt_tokens,
            total_tokens=prompt_tokens,
        )

    async def stream_chat(  # type: ignore[override]
        self, request: AdapterChatRequest
    ) -> AsyncIterator[AdapterChatChunk]:
        """Streaming 호출 (stream=true, NDJSON).

        마지막 done=true 응답에만 토큰/타이밍이 들어 있다. 놓치면 지표가 통째로 빈다.
        여기서 재시도하지 않는다 (Phase 6 의 상위 책임).
        """
        client = await self._ensure_client()
        payload = self._build_payload(request, stream=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.deployment.timeout.total
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout_at(deadline):
                response = await client.send(
                    client.build_request("POST", CHAT_PATH, json=payload), stream=True
                )
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
            lines = response.aiter_lines()
            while True:
                try:
                    if loop.time() >= deadline:
                        raise TimeoutError
                    async with asyncio.timeout_at(deadline):
                        line = await anext(lines)
                except StopAsyncIteration:
                    break
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise UpstreamProtocolError(
                        "upstream stream contained a non-JSON line"
                    ) from exc
                if not isinstance(data, dict):
                    raise UpstreamProtocolError("upstream stream line must be an object")
                if data.get("error"):
                    raise self._error_from_message(str(data["error"]), status=None)
                if data.get("done"):
                    yield AdapterChatChunk(
                        delta=self._content_of(data),
                        finish_reason=data.get("done_reason") or "stop",
                        usage=self._extract_usage(data),
                        timings=self._extract_timings(data),
                    )
                    return
                yield AdapterChatChunk(delta=self._content_of(data))
        except TimeoutError as exc:
            raise self._translate_exception(exc) from exc
        except httpx.HTTPError as exc:
            raise self._translate_exception(exc) from exc
        finally:
            if response is not None:
                await response.aclose()
        raise UpstreamProtocolError("upstream stream ended without done=true")

    async def health(self) -> bool:
        """endpoint 도달 가능 여부. 모델이 GPU 에 올라와 있는지는 보지 않는다."""
        client = await self._ensure_client()
        try:
            response = await client.get(TAGS_PATH, timeout=HEALTH_TIMEOUT_SEC)
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── 내부 ────────────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        """deployment 당 하나의 AsyncClient 를 만들어 재사용한다."""
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self.deployment.endpoint,
                        timeout=self._build_timeout(),
                    )
        return self._client

    def _build_payload(self, request: AdapterChatRequest, *, stream: bool) -> dict[str, Any]:
        """중립 DTO -> Ollama 요청 body.

        None 인 값은 options 에 넣지 않는다 (Ollama 가 null 을 싫어한다).
        """
        options: dict[str, Any] = {}
        for key, value in (
            ("temperature", request.temperature),
            ("top_p", request.top_p),
            ("num_predict", request.max_tokens),
            ("stop", request.stop),
            ("seed", request.seed),
            ("num_ctx", request.num_ctx),
        ):
            if value is not None:
                options[key] = value

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": stream,
        }
        if options:
            payload["options"] = options

        # structured output. 스키마를 주면 Ollama 가 그 모양 밖의 토큰을 생성하지 못한다.
        if request.json_schema is not None:
            payload["format"] = request.json_schema
        elif request.json_output:
            payload["format"] = "json"

        # cold start(진단 R6) 를 좌우하는 값이라 설정에서 조정할 수 있게 열어둔다.
        keep_alive = request.extra.get("keep_alive")
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive

        return payload

    def _parse_final(self, data: dict[str, Any]) -> AdapterChatResponse:
        """done=true 응답 -> AdapterChatResponse."""
        return AdapterChatResponse(
            content=self._content_of(data),
            finish_reason=data.get("done_reason") or "stop",
            usage=self._extract_usage(data),
            timings=self._extract_timings(data),
            upstream_model=data.get("model") or self.deployment.upstream_model,
            raw=data,
        )

    @staticmethod
    def _content_of(data: dict[str, Any]) -> str:
        message = data.get("message") or {}
        if not isinstance(message, dict):
            raise UpstreamProtocolError("upstream message must be an object")
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise UpstreamProtocolError("upstream content must be a string")
        return content

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> AdapterUsage:
        """prompt_eval_count -> input, eval_count -> output.

        키가 없으면 None 이다. 0 을 기본값으로 쓰면 "토큰 0개"로 집계된다.
        """
        return AdapterUsage(
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            source="upstream",
        )

    @staticmethod
    def _extract_timings(data: dict[str, Any]) -> AdapterTimings:
        """나노초 필드를 초로 변환한다.

        Ollama 는 queue 시간을 주지 않는다. queue_sec 는 None 으로 둔다.
        (추정치로 채우면 진단 규칙 R1/R2 가 틀린 판단을 한다)
        """
        return AdapterTimings(
            queue_sec=None,
            prompt_eval_sec=ns_to_sec(data.get("prompt_eval_duration")),
            generation_sec=ns_to_sec(data.get("eval_duration")),
            load_sec=ns_to_sec(data.get("load_duration")),
        )

    def _build_timeout(self) -> httpx.Timeout:
        """deployment.timeout -> httpx.Timeout.

        total 은 httpx 가 직접 지원하지 않으므로 호출부에서 asyncio.timeout 으로 감싼다.
        """
        return http_timeout(self.deployment.timeout)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """upstream HTTP 에러 -> GatewayError 변환. 정상 응답이면 아무것도 하지 않는다."""
        if response.status_code < 400:
            return

        try:
            body = response.json()
            message = str((body.get("error") or body) if isinstance(body, dict) else body)
        except (json.JSONDecodeError, ValueError):
            message = response.text[:500]

        raise OllamaAdapter._error_from_message(message, status=response.status_code)

    @staticmethod
    def _error_from_message(message: str, *, status: int | None) -> GatewayError:
        """upstream 이 준 메시지를 에러 코드로 분류한다."""
        lowered = message.lower()
        detail = {"upstream_status": status, "upstream_message": message[:500]}

        if "out of memory" in lowered or "cuda oom" in lowered:
            # 재시도하면 상황이 더 나빠진다. retryable=False 를 유지할 것.
            return OutOfMemoryError_(f"upstream is out of memory: {message}", detail=detail)
        if "loading" in lowered or "pulling" in lowered:
            return ModelLoadingError(f"upstream model is loading: {message}", detail=detail)
        if status == 404 and "not found" in lowered:
            # 논리 모델은 있는데 upstream 에 모델이 없는 상태 = 설정과 서버 불일치.
            return ModelNotFoundError(f"upstream model is not available: {message}", detail=detail)
        return UpstreamError(f"upstream returned an error: {message}", detail=detail)

    def _translate_exception(self, exc: BaseException) -> UpstreamError:
        """httpx / asyncio 예외 -> GatewayError. httpx 예외를 그대로 흘리지 않는다."""
        detail = {"deployment_id": self.deployment.id, "endpoint": self.deployment.endpoint}

        if isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout):
            return UpstreamConnectTimeoutError("upstream connect timed out", detail=detail)
        if isinstance(exc, httpx.ReadTimeout):
            return UpstreamReadTimeoutError("upstream read timed out", detail=detail)
        if isinstance(exc, httpx.WriteTimeout):
            return UpstreamTotalTimeoutError("upstream request write timed out", detail=detail)
        if isinstance(exc, httpx.ConnectError):
            return UpstreamUnavailableError("upstream is unreachable", detail=detail)
        if isinstance(exc, TimeoutError):
            # asyncio.timeout 이 터진 경우 = total timeout 초과.
            return UpstreamTotalTimeoutError(
                f"upstream exceeded total timeout of {self.deployment.timeout.total}s",
                detail=detail,
            )
        return UpstreamError(f"upstream call failed: {exc.__class__.__name__}", detail=detail)
