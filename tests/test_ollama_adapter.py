"""Ollama Adapter 매핑 테스트.

실제 Ollama 없이, 저장해둔 응답 샘플로 매핑만 검증한다.
매핑 표: docs/specs/adapter-interface.md "4. Ollama 매핑"
"""

from __future__ import annotations

import httpx
import pytest

from llm_gateway.adapters.base import AdapterChatRequest, AdapterMessage
from llm_gateway.adapters.ollama import OllamaAdapter
from llm_gateway.core.errors import (
    OutOfMemoryError_,
    UpstreamUnavailableError,
    is_retryable,
)
from llm_gateway.registry.models import ModelDeployment

# Ollama /api/chat 의 done=true 응답 샘플 (duration 은 나노초)
OLLAMA_FINAL_RESPONSE = {
    "model": "qwen2.5:7b",
    "message": {"role": "assistant", "content": ""},
    "done": True,
    "done_reason": "stop",
    "total_duration": 8_113_331_500,
    "load_duration": 6_396_458,
    "prompt_eval_count": 812,
    "prompt_eval_duration": 132_325_000,
    "eval_count": 431,
    "eval_duration": 7_963_400_000,
}


@pytest.fixture
def adapter() -> OllamaAdapter:
    return OllamaAdapter(
        ModelDeployment(
            id="qwen-7b@ollama",
            logical_model="qwen-7b",
            adapter="ollama",
            endpoint="http://localhost:11434",
            upstream_model="qwen2.5:7b",
        )
    )


def _request(**overrides) -> AdapterChatRequest:
    fields = {
        "model": "qwen2.5:7b",
        "messages": [AdapterMessage(role="user", content="안녕")],
    }
    fields.update(overrides)
    return AdapterChatRequest(**fields)


class TestUsageMapping:
    def test_token_counts_mapped(self):
        """prompt_eval_count -> input, eval_count -> output."""
        usage = OllamaAdapter._extract_usage(OLLAMA_FINAL_RESPONSE)

        assert usage.input_tokens == 812
        assert usage.output_tokens == 431
        assert usage.source == "upstream"

    def test_missing_counts_become_none_not_zero(self):
        """키가 없으면 None 이어야 한다.

        0 으로 채우면 "토큰 0개"로 집계되어 지표가 조용히 망가진다.
        """
        usage = OllamaAdapter._extract_usage({"done": True})

        assert usage.input_tokens is None
        assert usage.output_tokens is None


class TestTimingMapping:
    def test_nanoseconds_converted_to_seconds(self):
        """eval_duration 7_963_400_000ns -> 7.9634s.

        나노초/초 혼동이 이 매핑에서 가장 흔한 버그다.
        """
        timings = OllamaAdapter._extract_timings(OLLAMA_FINAL_RESPONSE)

        assert timings.generation_sec == pytest.approx(7.9634)
        assert timings.prompt_eval_sec == pytest.approx(0.132325)
        assert timings.load_sec == pytest.approx(0.006396458)

    def test_queue_sec_is_none(self):
        """Ollama 는 queue 시간을 주지 않는다. 추정치로 채우지 말 것."""
        assert OllamaAdapter._extract_timings(OLLAMA_FINAL_RESPONSE).queue_sec is None

    def test_missing_durations_become_none(self):
        timings = OllamaAdapter._extract_timings({"done": True})

        assert timings.generation_sec is None
        assert timings.load_sec is None


class TestPayloadMapping:
    def test_max_tokens_maps_to_num_predict(self, adapter):
        payload = adapter._build_payload(_request(max_tokens=2048), stream=True)

        assert payload["options"]["num_predict"] == 2048
        assert payload["stream"] is True
        assert payload["model"] == "qwen2.5:7b"
        assert payload["messages"] == [{"role": "user", "content": "안녕"}]

    def test_none_options_omitted(self, adapter):
        """None 인 옵션은 payload 에서 빠져야 한다."""
        payload = adapter._build_payload(_request(temperature=0.2), stream=False)

        assert payload["options"] == {"temperature": 0.2}
        assert "keep_alive" not in payload

    def test_keep_alive_from_extra(self, adapter):
        """extra.keep_alive 가 top-level keep_alive 로 나가야 한다 (cold start 방지)."""
        payload = adapter._build_payload(
            _request(extra={"keep_alive": "30m"}), stream=True
        )

        assert payload["keep_alive"] == "30m"
        assert "keep_alive" not in payload.get("options", {})


class TestResponseMapping:
    def test_final_response_parsed(self, adapter):
        response = adapter._parse_final(
            {**OLLAMA_FINAL_RESPONSE, "message": {"role": "assistant", "content": "안녕하세요"}}
        )

        assert response.content == "안녕하세요"
        assert response.finish_reason == "stop"
        assert response.upstream_model == "qwen2.5:7b"
        assert response.usage.output_tokens == 431


class TestErrorMapping:
    async def test_connection_refused_maps_to_gw5002(self, adapter):
        error = adapter._translate_exception(httpx.ConnectError("connection refused"))

        assert isinstance(error, UpstreamUnavailableError)
        assert error.code == "GW-5002"
        assert is_retryable(error, stream_started=False) is True

    async def test_read_timeout_not_retryable_after_stream_started(self, adapter):
        """이미 클라이언트가 일부를 봤으면 재시도하지 않는다."""
        error = adapter._translate_exception(httpx.ReadTimeout("read timed out"))

        assert error.code == "GW-5004"
        assert is_retryable(error, stream_started=False) is True
        assert is_retryable(error, stream_started=True) is False

    async def test_total_timeout_maps_to_gw5005(self, adapter):
        error = adapter._translate_exception(TimeoutError())

        assert error.code == "GW-5005"
        assert is_retryable(error, stream_started=False) is False

    def test_oom_maps_to_gw5008_and_is_not_retryable(self):
        """GPU OOM 재시도는 상황을 악화시킨다. retryable=False 를 확인한다."""
        error = OllamaAdapter._error_from_message(
            "CUDA error: out of memory", status=500
        )

        assert isinstance(error, OutOfMemoryError_)
        assert error.code == "GW-5008"
        assert error.retryable is False
        assert is_retryable(error, stream_started=False) is False

    def test_http_error_body_becomes_gateway_error(self, adapter):
        response = httpx.Response(
            500,
            json={"error": "something exploded"},
            request=httpx.Request("POST", "http://localhost:11434/api/chat"),
        )

        with pytest.raises(Exception) as exc:
            adapter._raise_for_status(response)

        assert exc.value.code == "GW-5001"
        # 상위 계층이 재시도 판정에 쓰는 값이다.
        assert exc.value.detail["upstream_status"] == 500

    def test_ok_status_does_not_raise(self, adapter):
        response = httpx.Response(
            200,
            json=OLLAMA_FINAL_RESPONSE,
            request=httpx.Request("POST", "http://localhost:11434/api/chat"),
        )

        assert adapter._raise_for_status(response) is None


class TestStreamParsing:
    async def test_ndjson_stream_is_parsed(self, adapter, monkeypatch):
        """streaming 응답은 SSE 가 아니라 NDJSON 이다."""
        lines = [
            '{"message":{"content":"안녕"},"done":false}',
            '{"message":{"content":"하세요"},"done":false}',
            "",
            '{"message":{"content":""},"done":true,"done_reason":"stop",'
            '"prompt_eval_count":812,"eval_count":431,"eval_duration":7963400000}',
        ]
        _install_fake_transport(adapter, monkeypatch, lines)

        chunks = [chunk async for chunk in adapter.stream_chat(_request())]

        assert [c.delta for c in chunks] == ["안녕", "하세요", ""]
        # 마지막 chunk 에만 토큰/타이밍이 실린다. 놓치면 지표가 통째로 빈다.
        assert chunks[-1].finish_reason == "stop"
        assert chunks[-1].usage.output_tokens == 431
        assert chunks[-1].timings.generation_sec == pytest.approx(7.9634)

    async def test_upstream_error_status_is_translated(self, adapter, monkeypatch):
        _install_fake_transport(adapter, monkeypatch, lines=None, status=500)

        with pytest.raises(Exception) as exc:
            [chunk async for chunk in adapter.stream_chat(_request())]

        assert exc.value.code == "GW-5001"


def _install_fake_transport(
    adapter: OllamaAdapter,
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str] | None,
    status: int = 200,
) -> None:
    """실제 소켓 없이 httpx 레벨에서 Ollama 응답을 흉내낸다."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(status, json={"error": "boom"})
        body = "\n".join(lines or []).encode("utf-8")
        return httpx.Response(status, content=body)

    client = httpx.AsyncClient(
        base_url=adapter.deployment.endpoint,
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(adapter, "_client", client)
