"""Prometheus Metrics - Phase 2.

Phase 1 에서는 자리만 잡아둔다. ChatService 가 이 함수들을 호출하도록 구조를 만들어두면
Phase 2 는 본문만 채우면 된다.

**Metric 이름/Label/bucket 은 docs/specs/metrics-spec.md 가 정본이다.**
코드를 먼저 고치지 말고 문서를 먼저 고칠 것.

Label 규칙 (반드시 지킬 것):
  허용: model, deployment_id, adapter, stream, status, error_type,
        finish_reason, token_source, experiment, variant
  금지: request_id, user_id, session_id, prompt, content, 자유 문자열 전부
        -> cardinality 폭발로 Prometheus 가 죽는다.
           개별 요청 추적은 Metric 이 아니라 Log/Trace 의 일이다.
"""

from __future__ import annotations

from ..core.timing import ChatTimings

# ── bucket 정의 (metrics-spec.md 와 동일하게 유지) ────────────────

# TTFT 는 앞쪽 구간을 촘촘하게. Total Latency 와 같은 bucket 을 쓰면 해상도가 죽는다.
TTFT_BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1, 1.5, 2, 3, 5, 8, 12, 20)
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
TPS_BUCKETS = (1, 2, 5, 10, 20, 30, 40, 50, 60, 80, 100, 150, 200)
INPUT_TOKEN_BUCKETS = (128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
OUTPUT_TOKEN_BUCKETS = (16, 32, 64, 128, 256, 512, 1024, 2048, 4096)


# TODO(Phase 2): prometheus_client Counter/Histogram/Gauge 정의
#
#   REQUESTS_TOTAL = Counter(
#       "llm_gateway_requests_total", "...",
#       ["model", "deployment_id", "adapter", "stream", "status"],
#   )
#   ERRORS_TOTAL = Counter(
#       "llm_gateway_errors_total", "...",
#       ["model", "deployment_id", "error_type", "code"],
#   )
#   REQUEST_DURATION = Histogram(..., buckets=LATENCY_BUCKETS)
#   TTFT = Histogram("llm_gateway_ttft_seconds", ..., buckets=TTFT_BUCKETS)
#   GENERATION_DURATION = Histogram(..., buckets=LATENCY_BUCKETS)
#   OUTPUT_TPS = Histogram(..., buckets=TPS_BUCKETS)
#   INPUT_TOKENS_TOTAL / OUTPUT_TOKENS_TOTAL = Counter(..., [..., "token_source"])
#   INPUT_TOKENS / OUTPUT_TOKENS = Histogram(..., buckets=..._TOKEN_BUCKETS)
#   FINISH_REASON_TOTAL = Counter(...)
#   INFLIGHT = Gauge(...)


def record_request(
    *,
    model: str,
    deployment_id: str,
    adapter: str,
    stream: bool,
    status: str,
    timings: ChatTimings,
    input_tokens: int | None,
    output_tokens: int | None,
    token_source: str,
    finish_reason: str | None,
) -> None:
    """한 요청의 계측을 기록한다. ChatService 가 응답 직후 한 번 호출한다.

    TODO(Phase 2): 구현.

    주의:
      - None 인 값은 **관측하지 않는다.** 0 으로 대체하면 히스토그램이 왜곡된다.
        (upstream 이 토큰을 안 준 것과 토큰이 0개인 것은 다르다)
      - TPS 는 timings.output_tps(output_tokens) 로 계산하고, None 이면 건너뛴다.
      - stream 은 문자열 "true"/"false" 로 넣는다 (label 은 문자열이어야 한다).
    """
    raise NotImplementedError


def record_error(
    *, model: str, deployment_id: str | None, error_type: str, code: str
) -> None:
    """TODO(Phase 2): 에러 카운터 증가.

    error_type 은 core.errors.ErrorType 열거형 값만 사용한다.
    upstream 에러 메시지 원문을 넣지 말 것.
    """
    raise NotImplementedError


def inflight_tracker(model: str, deployment_id: str):
    """TODO(Phase 2): 진행 중 요청 수 Gauge 를 관리하는 async context manager.

    Ollama 는 큐 깊이를 노출하지 않으므로, 이 값이 L3 지표의 대체재가 된다
    (docs/operations/observability-stack.md "4.3 Serving Dashboard").
    """
    raise NotImplementedError


def metrics_asgi_app():
    """TODO(Phase 2): prometheus_client.make_asgi_app() 반환.

    main.py 에서 app.mount("/metrics", metrics_asgi_app()) 로 붙인다.
    settings.metrics_enabled 가 False 면 mount 하지 않는다.
    """
    raise NotImplementedError
