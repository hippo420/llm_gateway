"""POST /v1/chat/completions 계약 테스트.

여기서 검증하는 것은 "LLM 이 좋은 답을 하는가"가 아니라
**Spring AI 가 파싱할 수 있는 응답을 주는가**이다.
"""

from __future__ import annotations

import json

from llm_gateway.core.context import RequestContext
from llm_gateway.core.errors import UpstreamError
from llm_gateway.schemas.chat import ChatCompletionRequest
from llm_gateway.service.chat_service import ChatService

from .conftest import FakeAdapter

BODY = {
    "model": "qwen-7b",
    "messages": [{"role": "user", "content": "삼성전자 최근 실적을 분석해줘"}],
}


def _sse_events(text: str) -> list[str]:
    """SSE 본문 -> 이벤트 payload 목록. 형식 위반이 있으면 여기서 드러난다."""
    events = []
    for block in text.split("\n\n"):
        if not block:
            continue
        assert block.startswith("data: "), f"SSE event must start with 'data: ': {block!r}"
        events.append(block[len("data: ") :])
    return events


class TestNonStreaming:
    async def test_returns_openai_shape(self, client):
        """id/object/created/model/choices/usage 가 모두 있어야 한다.

        하나라도 빠지면 Spring AI 가 파싱에 실패한다.
        """
        response = await client.post("/v1/chat/completions", json=BODY)

        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {"id", "object", "created", "model", "choices", "usage"}
        assert body["object"] == "chat.completion"
        assert isinstance(body["created"], int)

        choice = body["choices"][0]
        assert choice["index"] == 0
        assert choice["finish_reason"] == "stop"
        assert choice["message"] == {"role": "assistant", "content": "안녕하세요"}

        assert body["usage"] == {
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "total_tokens": 17,
        }

    async def test_returns_logical_model_name(self, client):
        """응답 body 의 model 은 **논리 모델명**이어야 한다 (upstream 모델명이 아니라).

        실제 deployment 는 X-Gateway-Deployment 헤더로만 노출된다.
        이게 깨지면 논리/물리 분리가 무너진 것이다.
        """
        response = await client.post("/v1/chat/completions", json=BODY)

        assert response.json()["model"] == "qwen-7b"
        assert "qwen2.5:7b" not in response.text
        assert response.headers["X-Gateway-Deployment"] == "qwen-7b@fake"

    async def test_unknown_model_returns_gw4001(self, client):
        """등록되지 않은 모델 -> 404 + GW-4001 model_not_found."""
        response = await client.post(
            "/v1/chat/completions", json={**BODY, "model": "qwen-70b"}
        )

        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "GW-4001"
        assert error["type"] == "model_not_found"
        assert error["request_id"]

    async def test_all_disabled_returns_gw4004(self, client):
        """후보가 전부 disabled 면 503 + GW-4004."""
        response = await client.post(
            "/v1/chat/completions", json={**BODY, "model": "qwen-off"}
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "GW-4004"

    async def test_empty_messages_returns_gw4000(self, client):
        """messages 가 비면 -> 400 + GW-4000."""
        response = await client.post("/v1/chat/completions", json={**BODY, "messages": []})

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "GW-4000"
        assert error["type"] == "invalid_request"
        # 검증 에러 상세에 프롬프트가 새면 안 된다. 필드 이름만 남는다.
        assert "삼성전자" not in response.text

    async def test_unsupported_parameter_returns_gw4002(self, client):
        """tools 를 조용히 무시하지 않고 거절한다."""
        response = await client.post(
            "/v1/chat/completions",
            json={**BODY, "tools": [{"type": "function"}]},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "GW-4002"

    async def test_deployment_defaults_applied(self, client, fake_adapter):
        """요청에서 temperature 를 생략하면 deployment 기본값이 adapter 까지 전달돼야 한다.

        생략된 필드가 None 으로 기본값을 덮어쓰는 버그가 자주 난다.
        """
        await client.post("/v1/chat/completions", json=BODY)

        call = fake_adapter.calls[0]
        assert call.temperature == 0.2
        assert call.top_p == 0.9
        assert call.max_tokens == 2048
        # adapter 는 논리명이 아니라 upstream 모델명을 받는다.
        assert call.model == "qwen2.5:7b"
        assert call.extra["keep_alive"] == "30m"

    async def test_request_overrides_deployment_defaults(self, client, fake_adapter):
        """명시된 요청 파라미터는 deployment 기본값을 이긴다."""
        await client.post("/v1/chat/completions", json={**BODY, "temperature": 0.9})

        call = fake_adapter.calls[0]
        assert call.temperature == 0.9
        assert call.max_tokens == 2048  # 생략한 값은 그대로 남는다


class TestStreaming:
    async def test_sse_format(self, client):
        """각 이벤트가 'data: ' 로 시작하고 빈 줄로 끝나며, 마지막이 [DONE] 이어야 한다."""
        response = await client.post("/v1/chat/completions", json={**BODY, "stream": True})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = _sse_events(response.text)
        assert events[-1] == "[DONE]"

        first = json.loads(events[0])
        assert first["object"] == "chat.completion.chunk"
        assert first["model"] == "qwen-7b"
        assert first["choices"][0]["delta"] == {"role": "assistant"}

        content = "".join(
            json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1]
        )
        assert content == "안녕하세요"

    async def test_stream_alias_forces_streaming(self, client):
        """/v1/chat/stream 은 stream 값과 무관하게 SSE 로 응답한다."""
        response = await client.post("/v1/chat/stream", json=BODY)

        assert response.headers["content-type"].startswith("text/event-stream")
        assert _sse_events(response.text)[-1] == "[DONE]"

    async def test_last_chunk_has_finish_reason(self, client):
        """마지막 chunk 에 finish_reason 이 있어야 한다."""
        response = await client.post("/v1/chat/completions", json={**BODY, "stream": True})

        events = _sse_events(response.text)
        final = json.loads(events[-2])
        assert final["choices"][0]["finish_reason"] == "stop"
        assert final["usage"]["completion_tokens"] == 5

    async def test_error_mid_stream_closes_cleanly(self, client, app, deployment):
        """스트림 도중 에러: 에러 chunk + [DONE] 으로 닫아야 한다.

        HTTP 상태는 이미 200 이라 바꿀 수 없다.
        """
        app.state.adapters.register(
            deployment.id,
            FakeAdapter(deployment, error=UpstreamError("upstream connection lost")),
        )

        response = await client.post("/v1/chat/completions", json={**BODY, "stream": True})

        assert response.status_code == 200
        events = _sse_events(response.text)
        assert events[-1] == "[DONE]"

        error = json.loads(events[-2])["error"]
        assert error["code"] == "GW-5001"
        assert error["type"] == "upstream_error"


class TestRequestId:
    async def test_generated_when_absent(self, client):
        """헤더가 없으면 Gateway 가 만들어 응답 헤더로 돌려준다."""
        response = await client.post("/v1/chat/completions", json=BODY)

        assert response.headers["X-Request-Id"]

    async def test_propagated_when_present(self, client):
        """Spring 이 보낸 X-Request-Id 를 그대로 승계해야 한다. correlation 의 전제."""
        response = await client.post(
            "/v1/chat/completions", json=BODY, headers={"X-Request-Id": "spring-abc-123"}
        )

        assert response.headers["X-Request-Id"] == "spring-abc-123"
        assert response.json()["id"] == "chatcmpl-spring-a"

    async def test_unsafe_incoming_id_is_replaced(self, client):
        """개행이 섞인 값은 승계하지 않는다 (로그 오염/주입 방지)."""
        response = await client.post(
            "/v1/chat/completions", json=BODY, headers={"X-Request-Id": "abc\tdef"}
        )

        assert response.headers["X-Request-Id"] != "abc\tdef"

    async def test_present_on_error_response(self, client):
        """**에러 응답에도** X-Request-Id 가 있어야 한다. 빠뜨리기 쉬운 경로다."""
        response = await client.post(
            "/v1/chat/completions",
            json={**BODY, "model": "qwen-70b"},
            headers={"X-Request-Id": "spring-err-1"},
        )

        assert response.status_code == 404
        assert response.headers["X-Request-Id"] == "spring-err-1"
        assert response.json()["error"]["request_id"] == "spring-err-1"


class TestTiming:
    async def test_ttft_measured_from_first_non_empty_chunk(self, registry, deployment):
        """role 만 담긴 빈 chunk 를 첫 토큰으로 세면 안 된다.

        FakeAdapter(first_token_delay=0.2) 로 검증한다.
        """
        from llm_gateway.adapters.factory import AdapterFactory

        adapters = AdapterFactory()
        adapters.register(
            deployment.id, FakeAdapter(deployment, first_token_delay=0.2)
        )
        service = ChatService(registry, adapters)
        ctx = RequestContext(request_id="test-ttft")

        result = await service.complete(ChatCompletionRequest(**BODY), ctx)

        assert result.timings.ttft_sec is not None
        assert result.timings.ttft_sec >= 0.2
        assert result.timings.total_sec >= result.timings.ttft_sec
        # upstream 이 준 원자료도 함께 살아있어야 Phase 2 가 쓸 수 있다.
        assert result.timings.prompt_eval_sec == 0.13
        # Ollama 는 queue 시간을 주지 않는다. 추정치로 채우지 않는다.
        assert result.timings.queue_sec is None
