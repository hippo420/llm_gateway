"""Ollama Adapter 매핑 테스트.

실제 Ollama 없이, 저장해둔 응답 샘플로 매핑만 검증한다.
매핑 표: docs/specs/adapter-interface.md "4. Ollama 매핑"
"""

from __future__ import annotations

import pytest

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


class TestUsageMapping:
    @pytest.mark.skip(reason="TODO: Phase 1 구현 후 활성화")
    def test_token_counts_mapped(self):
        """prompt_eval_count -> input, eval_count -> output."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_missing_counts_become_none_not_zero(self):
        """키가 없으면 None 이어야 한다.

        0 으로 채우면 "토큰 0개"로 집계되어 지표가 조용히 망가진다.
        """
        raise NotImplementedError


class TestTimingMapping:
    @pytest.mark.skip(reason="TODO")
    def test_nanoseconds_converted_to_seconds(self):
        """eval_duration 7_963_400_000ns -> 7.9634s.

        나노초/초 혼동이 이 매핑에서 가장 흔한 버그다.
        """
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_queue_sec_is_none(self):
        """Ollama 는 queue 시간을 주지 않는다. 추정치로 채우지 말 것."""
        raise NotImplementedError


class TestPayloadMapping:
    @pytest.mark.skip(reason="TODO")
    def test_max_tokens_maps_to_num_predict(self):
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_none_options_omitted(self):
        """None 인 옵션은 payload 에서 빠져야 한다."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_keep_alive_from_extra(self):
        """extra.keep_alive 가 top-level keep_alive 로 나가야 한다 (cold start 방지)."""
        raise NotImplementedError


class TestErrorMapping:
    @pytest.mark.skip(reason="TODO")
    def test_connection_refused_maps_to_gw5002(self):
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_oom_maps_to_gw5008_and_is_not_retryable(self):
        """GPU OOM 재시도는 상황을 악화시킨다. retryable=False 를 확인한다."""
        raise NotImplementedError
