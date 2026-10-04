"""OpenAI-compatible chat transport for LM Studio. Endpoint includes /v1."""

from __future__ import annotations

import asyncio
import json
import os
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
from ..registry.models import ModelDeployment
from ..resilience.timeout import http_timeout
from .base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
    AdapterTimings,
    AdapterUsage,
    LLMAdapter,
)


class OpenAICompatibleAdapter(LLMAdapter):
    name = "openai"

    def __init__(self, deployment: ModelDeployment) -> None:
        super().__init__(deployment)
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    headers = {}
                    if self.deployment.api_key_env:
                        key = os.environ.get(self.deployment.api_key_env)
                        if not key:
                            raise UpstreamUnavailableError("upstream API key is not configured")
                        headers["Authorization"] = f"Bearer {key}"
                    self._client = httpx.AsyncClient(
                        base_url=self.deployment.endpoint.rstrip("/") + "/",
                        headers=headers,
                        timeout=http_timeout(self.deployment.timeout),
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _build_payload(request: AdapterChatRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": stream,
        }
        for key in ("temperature", "top_p", "max_tokens", "stop", "seed"):
            value = getattr(request, key)
            if value is not None:
                payload[key] = value
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": request.json_schema},
            }
        elif request.json_output:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _decode(body: str) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise UpstreamProtocolError("upstream returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise UpstreamProtocolError("upstream response must be an object")
        if data.get("error"):
            raise UpstreamError("upstream returned a stream error")
        return data

    @staticmethod
    def _usage(data: dict[str, Any]) -> AdapterUsage | None:
        usage = data.get("usage")
        if usage is None:
            return None
        if not isinstance(usage, dict):
            raise UpstreamProtocolError("upstream usage must be an object")
        return AdapterUsage(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    @staticmethod
    def _choice(data: dict[str, Any]) -> dict[str, Any] | None:
        choices = data.get("choices")
        if not isinstance(choices, list):
            raise UpstreamProtocolError("upstream choices must be an array")
        if not choices:
            return None
        if not isinstance(choices[0], dict):
            raise UpstreamProtocolError("upstream choice must be an object")
        return choices[0]

    @staticmethod
    def _content(choice: dict[str, Any], key: str) -> str:
        message = choice.get(key)
        if not isinstance(message, dict):
            raise UpstreamProtocolError("upstream message must be an object")
        content = message.get("content")
        if content is None:
            return ""
        if not isinstance(content, str):
            raise UpstreamProtocolError("upstream content must be a string")
        return content

    async def chat(self, request: AdapterChatRequest) -> AdapterChatResponse:
        client = await self._ensure_client()
        try:
            async with asyncio.timeout(self.deployment.timeout.total):
                response = await client.post(
                    "chat/completions",
                    json=self._build_payload(request, stream=False),
                )
                self._raise_for_status(response)
                data = self._decode(response.text)
        except (TimeoutError, httpx.HTTPError) as exc:
            raise self._translate_exception(exc) from exc
        choice = self._choice(data)
        if choice is None or not choice.get("finish_reason"):
            raise UpstreamProtocolError("upstream response has no completed choice")
        return AdapterChatResponse(
            content=self._content(choice, "message"),
            finish_reason=choice["finish_reason"],
            usage=self._usage(data) or AdapterUsage(),
            timings=AdapterTimings(),
            upstream_model=data.get("model") or request.model,
            raw=data,
        )

    async def stream_chat(self, request: AdapterChatRequest) -> AsyncIterator[AdapterChatChunk]:
        client = await self._ensure_client()
        deadline = asyncio.get_running_loop().time() + self.deployment.timeout.total
        response = None
        finish_reason = None
        usage = None
        try:
            async with asyncio.timeout_at(deadline):
                response = await client.send(
                    client.build_request(
                        "POST",
                        "chat/completions",
                        json=self._build_payload(request, stream=True),
                    ),
                    stream=True,
                )
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
            lines = response.aiter_lines()
            fields: list[str] = []
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        line = await anext(lines)
                except StopAsyncIteration:
                    break
                if line.startswith("data:"):
                    fields.append(line[5:].lstrip(" "))
                if line or not fields:
                    continue
                body = "\n".join(fields)
                fields.clear()
                if body.strip() == "[DONE]":
                    if finish_reason is None:
                        raise UpstreamProtocolError("upstream stream has no finish reason")
                    yield AdapterChatChunk(
                        finish_reason=finish_reason,
                        usage=usage,
                        timings=AdapterTimings(),
                    )
                    return
                data = self._decode(body)
                current_usage = self._usage(data)
                if current_usage is not None:
                    usage = current_usage
                choice = self._choice(data)
                if choice is not None:
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = self._content(choice, "delta")
                    if delta:
                        yield AdapterChatChunk(delta=delta)
        except (TimeoutError, httpx.HTTPError) as exc:
            raise self._translate_exception(exc) from exc
        finally:
            if response is not None:
                await response.aclose()
        raise UpstreamProtocolError("upstream stream ended without [DONE]")

    async def health(self) -> bool:
        try:
            client = await self._ensure_client()
            response = await client.get("models", timeout=2)
            if response.status_code >= 400:
                return False
            data = self._decode(response.text).get("data")
            return isinstance(data, list) and any(
                isinstance(model, dict) and model.get("id") == self.deployment.upstream_model
                for model in data
            )
        except (httpx.HTTPError, GatewayError):
            return False

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

        raise OpenAICompatibleAdapter._error_from_message(message, status=response.status_code)

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
