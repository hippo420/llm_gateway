"""POST /v1/chat/completions 계약 테스트.

여기서 검증하는 것은 "LLM 이 좋은 답을 하는가"가 아니라
**Spring AI 가 파싱할 수 있는 응답을 주는가**이다.
"""

from __future__ import annotations

import pytest


class TestNonStreaming:
    @pytest.mark.skip(reason="TODO: Phase 1 구현 후 활성화")
    async def test_returns_openai_shape(self, client):
        """id/object/created/model/choices/usage 가 모두 있어야 한다.

        하나라도 빠지면 Spring AI 가 파싱에 실패한다.
        """
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_returns_logical_model_name(self, client):
        """응답 body 의 model 은 **논리 모델명**이어야 한다 (upstream 모델명이 아니라).

        실제 deployment 는 X-Gateway-Deployment 헤더로만 노출된다.
        이게 깨지면 논리/물리 분리가 무너진 것이다.
        """
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_unknown_model_returns_gw4001(self, client):
        """등록되지 않은 모델 -> 404 + GW-4001 model_not_found."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_empty_messages_returns_gw4000(self, client):
        """messages 가 비면 -> 400 + GW-4000."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_deployment_defaults_applied(self, client):
        """요청에서 temperature 를 생략하면 deployment 기본값이 adapter 까지 전달돼야 한다.

        생략된 필드가 None 으로 기본값을 덮어쓰는 버그가 자주 난다.
        """
        raise NotImplementedError


class TestStreaming:
    @pytest.mark.skip(reason="TODO")
    async def test_sse_format(self, client):
        """각 이벤트가 'data: ' 로 시작하고 빈 줄로 끝나며, 마지막이 [DONE] 이어야 한다."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_last_chunk_has_finish_reason(self, client):
        """마지막 chunk 에 finish_reason 이 있어야 한다."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_error_mid_stream_closes_cleanly(self, client):
        """스트림 도중 에러: 에러 chunk + [DONE] 으로 닫아야 한다.

        HTTP 상태는 이미 200 이라 바꿀 수 없다.
        """
        raise NotImplementedError


class TestRequestId:
    @pytest.mark.skip(reason="TODO")
    async def test_generated_when_absent(self, client):
        """헤더가 없으면 Gateway 가 만들어 응답 헤더로 돌려준다."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_propagated_when_present(self, client):
        """Spring 이 보낸 X-Request-Id 를 그대로 승계해야 한다. correlation 의 전제."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    async def test_present_on_error_response(self, client):
        """**에러 응답에도** X-Request-Id 가 있어야 한다. 빠뜨리기 쉬운 경로다."""
        raise NotImplementedError


class TestTiming:
    @pytest.mark.skip(reason="TODO: Phase 2")
    async def test_ttft_measured_from_first_non_empty_chunk(self, client):
        """role 만 담긴 빈 chunk 를 첫 토큰으로 세면 안 된다.

        FakeAdapter(first_token_delay=0.2) 로 검증한다.
        """
        raise NotImplementedError
