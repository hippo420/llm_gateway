"""LLM 특화 시간 측정.

이 모듈이 Phase 2 의 데이터 원천이다. Phase 1 에서 자리를 정확히 잡아두면
Phase 2 는 여기서 나온 값을 Prometheus 에 넘기기만 하면 된다.

    Total Latency
     ├── Queue Latency        (upstream 이 줄 때만)
     ├── Prompt Processing    (prefill)
     ├── TTFT                 (= queue + prefill + a)
     └── Generation           (decode)

측정 정의: docs/phases/phase-02-instrumentation.md "3. 측정 방법"
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ChatTimings:
    """한 요청의 타이밍 측정 결과. 전부 초 단위.

    None 은 "모른다"는 뜻이다. **0 으로 채우지 않는다.**
    0 은 "실제로 0초였다"는 뜻이고, 지표에서 완전히 다르게 해석된다.
    """

    total_sec: float | None = None
    # content 가 비어있지 않은 첫 chunk 까지. non-streaming 호출에서는 None.
    ttft_sec: float | None = None
    # total - ttft
    generation_sec: float | None = None
    # 아래는 upstream 이 제공할 때만 채운다
    queue_sec: float | None = None
    prompt_eval_sec: float | None = None
    load_sec: float | None = None

    def output_tps(self, output_tokens: int | None) -> float | None:
        """Output Tokens Per Second.

        generation_sec 이 없거나 0 에 매우 가까우면 None 을 반환한다.
        (아주 짧은 응답에서 division guard 없이 계산하면 TPS 가 수천으로 튄다)
        """
        return _safe_rate(output_tokens, self.generation_sec)

    def input_tps(self, input_tokens: int | None) -> float | None:
        """Input(prefill) Tokens Per Second. prompt_eval_sec 이 없으면 None."""
        return _safe_rate(input_tokens, self.prompt_eval_sec)


# 이보다 짧은 구간으로 나눈 TPS 는 노이즈다. 계산하지 않고 None 을 돌려준다.
_MIN_MEASURABLE_SEC = 1e-3


def _safe_rate(tokens: int | None, seconds: float | None) -> float | None:
    if tokens is None or seconds is None or seconds < _MIN_MEASURABLE_SEC:
        return None
    return tokens / seconds


class Stopwatch:
    """단조 시계 기반 구간 측정기.

    time.time() 이 아니라 time.perf_counter() 를 쓴다.
    시스템 시각 보정(NTP)이 latency 측정을 오염시키면 안 된다.

    사용:
        sw = Stopwatch().start()
        ...
        sw.mark_first_token()      # content 있는 첫 chunk 에서 한 번만
        ...
        timings = sw.finish()
    """

    def __init__(self) -> None:
        self._started_at: float | None = None
        self._first_token_at: float | None = None
        self._finished_at: float | None = None

    def start(self) -> Stopwatch:
        self._started_at = _monotonic()
        return self

    def mark_first_token(self) -> None:
        """최초 1회만 기록한다 (두 번째 이후 호출은 무시).

        호출 위치가 중요하다. OpenAI 호환 스트림의 첫 chunk 는 delta.role 만 담고
        content 가 비어 있는 경우가 많다. 그것을 첫 토큰으로 세면 TTFT 가 실제보다 짧게 나온다.
        -> **delta 문자열이 비어있지 않을 때만** 호출할 것.
        """
        if self._first_token_at is None:
            self._first_token_at = _monotonic()

    def finish(self) -> ChatTimings:
        """total_sec / ttft_sec / generation_sec 을 채운 ChatTimings 를 만든다."""
        if self._started_at is None:
            raise RuntimeError("Stopwatch.start() was not called")
        if self._finished_at is None:
            self._finished_at = _monotonic()

        total_sec = self._finished_at - self._started_at
        ttft_sec = (
            None if self._first_token_at is None else self._first_token_at - self._started_at
        )
        # 첫 토큰을 못 본 호출(non-streaming)에서는 generation 을 분리할 수 없다.
        generation_sec = None if ttft_sec is None else total_sec - ttft_sec

        return ChatTimings(
            total_sec=total_sec,
            ttft_sec=ttft_sec,
            generation_sec=generation_sec,
        )

    @property
    def elapsed_sec(self) -> float:
        """시작 이후 경과 시간 (finish 전에도 조회 가능)."""
        if self._started_at is None:
            raise RuntimeError("Stopwatch.start() was not called")
        end = self._finished_at if self._finished_at is not None else _monotonic()
        return end - self._started_at


def ns_to_sec(value: int | None) -> float | None:
    """나노초 -> 초.

    Ollama 의 duration 필드(total_duration, load_duration, eval_duration ...)는
    전부 나노초다. 매핑에서 가장 자주 나는 실수라 헬퍼로 고정해둔다.
    """
    return None if value is None else value / 1e9


def _monotonic() -> float:
    """단조 시계. Stopwatch 내부에서만 사용."""
    return time.perf_counter()


__all__ = ["ChatTimings", "Stopwatch", "ns_to_sec"]
