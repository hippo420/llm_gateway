"""Prometheus Metrics - Phase 2.

**Metric 이름/Label/bucket 은 docs/specs/metrics-spec.md 가 정본이다.**
코드를 먼저 고치지 말고 문서를 먼저 고칠 것.

Label 규칙 (반드시 지킬 것):
  허용: model, deployment_id, adapter, stream, status, error_type,
        finish_reason, token_source, experiment, variant
  금지: request_id, user_id, session_id, prompt, content, 자유 문자열 전부
        -> cardinality 폭발로 Prometheus 가 죽는다.
           개별 요청 추적은 Metric 이 아니라 Log/Trace 의 일이다.

기록 책임 (누가 무엇을 부르는가):
  ChatService   deployment 선택 이후의 모든 것 (성공/실패/취소, inflight)
  prepare()     선택 단계에서 난 에러 (model_not_found 등) -> record_error 만
  에러 핸들러    위 둘이 기록하지 않은 에러 (인증/본문 검증) -> record_error 만
"""

from __future__ import annotations

from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.context_managers import InprogressTracker
from starlette.requests import Request
from starlette.responses import Response

from ..core.timing import ChatTimings

# Counter/Histogram 마다 따라붙는 *_created 시계열을 끈다.
# 쓰는 곳이 없는데 시계열 수만 늘린다 (cardinality 규칙).
disable_created_metrics()

# ── bucket 정의 (metrics-spec.md 와 동일하게 유지) ────────────────

# TTFT 는 앞쪽 구간을 촘촘하게. Total Latency 와 같은 bucket 을 쓰면 해상도가 죽는다.
TTFT_BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1, 1.5, 2, 3, 5, 8, 12, 20)
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
GENERATION_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
TPS_BUCKETS = (1, 2, 5, 10, 20, 30, 40, 50, 60, 80, 100, 150, 200)
INPUT_TOKEN_BUCKETS = (128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
OUTPUT_TOKEN_BUCKETS = (16, 32, 64, 128, 256, 512, 1024, 2048, 4096)

# ── label 값 정규화 ──────────────────────────────────────────────

# deployment 가 정해지기 전에 난 에러의 deployment_id / adapter.
NO_DEPLOYMENT: Final = "none"
# 등록되지 않은 모델명. 클라이언트가 보낸 문자열을 그대로 label 에 넣으면
# 오타 하나마다 시계열이 생긴다 (= 자유 문자열 금지 규칙).
UNKNOWN_MODEL: Final = "unknown"

STATUS_SUCCESS: Final = "success"
STATUS_ERROR: Final = "error"
# 클라이언트 이탈. 실패가 아니므로 error rate 에서 빠지도록 따로 둔다 (GW-4007).
STATUS_CANCELLED: Final = "cancelled"

FINISH_REASONS: Final = frozenset({"stop", "length", "error", "cancelled"})
# upstream 이 위 집합 밖의 값을 주면 (Ollama done_reason="load" 등) 여기로 모은다.
FINISH_REASON_OTHER: Final = "other"

# ── L1 Application ──────────────────────────────────────────────

REQUESTS_TOTAL = Counter(
    "llm_gateway_requests_total",
    "Chat requests that reached a deployment.",
    ["model", "deployment_id", "adapter", "stream", "status"],
)
ERRORS_TOTAL = Counter(
    "llm_gateway_errors_total",
    "Gateway errors by error-codes.md enum.",
    ["model", "deployment_id", "error_type", "code"],
)
REQUEST_DURATION = Histogram(
    "llm_gateway_request_duration_seconds",
    "Adapter call start to last chunk, successful requests only.",
    ["model", "deployment_id", "stream"],
    buckets=LATENCY_BUCKETS,
)
INFLIGHT = Gauge(
    "llm_gateway_inflight_requests",
    "Chat requests currently being served.",
    ["model", "deployment_id"],
)

# ── L2 LLM ──────────────────────────────────────────────────────

TTFT = Histogram(
    "llm_gateway_ttft_seconds",
    "Time to first non-empty content chunk.",
    ["model", "deployment_id", "adapter"],
    buckets=TTFT_BUCKETS,
)
GENERATION_DURATION = Histogram(
    "llm_gateway_generation_duration_seconds",
    "Total latency minus TTFT (decode phase).",
    ["model", "deployment_id"],
    buckets=GENERATION_BUCKETS,
)
QUEUE_DURATION = Histogram(
    "llm_gateway_queue_duration_seconds",
    "Upstream queue wait, only when the serving framework reports it.",
    ["model", "deployment_id"],
    buckets=TTFT_BUCKETS,
)
OUTPUT_TPS = Histogram(
    "llm_gateway_output_tokens_per_second",
    "Per-request output tokens / generation seconds.",
    ["model", "deployment_id", "adapter"],
    buckets=TPS_BUCKETS,
)
INPUT_TOKENS_TOTAL = Counter(
    "llm_gateway_input_tokens_total",
    "Input (prompt) tokens.",
    ["model", "deployment_id", "token_source"],
)
OUTPUT_TOKENS_TOTAL = Counter(
    "llm_gateway_output_tokens_total",
    "Output (completion) tokens.",
    ["model", "deployment_id", "token_source"],
)
# 이름에 request_ 를 붙인 이유: Counter "..._input_tokens_total" 의 family 이름이
# "..._input_tokens" 라서, 같은 이름의 Histogram 은 등록 자체가 안 된다 (OpenMetrics 규칙).
INPUT_TOKENS = Histogram(
    "llm_gateway_request_input_tokens",
    "Input tokens per request.",
    ["model", "deployment_id"],
    buckets=INPUT_TOKEN_BUCKETS,
)
OUTPUT_TOKENS = Histogram(
    "llm_gateway_request_output_tokens",
    "Output tokens per request.",
    ["model", "deployment_id"],
    buckets=OUTPUT_TOKEN_BUCKETS,
)
FINISH_REASON_TOTAL = Counter(
    "llm_gateway_finish_reason_total",
    "Finish reasons. A high 'length' ratio means max_tokens is too small.",
    ["model", "deployment_id", "finish_reason"],
)


def record_request(
    *,
    model: str,
    deployment_id: str,
    adapter: str,
    stream: bool,
    status: str,
    timings: ChatTimings | None,
    input_tokens: int | None,
    output_tokens: int | None,
    token_source: str,
    finish_reason: str | None,
) -> None:
    """한 요청의 계측을 기록한다. ChatService 가 요청 종료 시 한 번 호출한다.

    - None 인 값은 **관측하지 않는다.** 0 으로 대체하면 히스토그램이 왜곡된다.
      (upstream 이 토큰을 안 준 것과 토큰이 0개인 것은 다르다)
    - latency/TTFT/TPS/토큰 분포는 **성공한 요청만** 관측한다.
      연결 거부처럼 즉시 끝난 실패가 섞이면 P95 가 실제보다 좋아 보인다.
      실패는 requests_total{status="error"} 와 errors_total 로 본다.
    """
    stream_label = "true" if stream else "false"
    REQUESTS_TOTAL.labels(model, deployment_id, adapter, stream_label, status).inc()
    if finish_reason is not None:
        FINISH_REASON_TOTAL.labels(
            model, deployment_id, normalize_finish_reason(finish_reason)
        ).inc()

    if status != STATUS_SUCCESS or timings is None:
        return

    if timings.total_sec is not None:
        REQUEST_DURATION.labels(model, deployment_id, stream_label).observe(timings.total_sec)
    if timings.ttft_sec is not None:
        TTFT.labels(model, deployment_id, adapter).observe(timings.ttft_sec)
    if timings.generation_sec is not None:
        GENERATION_DURATION.labels(model, deployment_id).observe(timings.generation_sec)
    if timings.queue_sec is not None:
        QUEUE_DURATION.labels(model, deployment_id).observe(timings.queue_sec)

    tps = timings.output_tps(output_tokens)
    if tps is not None:
        OUTPUT_TPS.labels(model, deployment_id, adapter).observe(tps)

    if input_tokens is not None:
        INPUT_TOKENS_TOTAL.labels(model, deployment_id, token_source).inc(input_tokens)
        INPUT_TOKENS.labels(model, deployment_id).observe(input_tokens)
    if output_tokens is not None:
        OUTPUT_TOKENS_TOTAL.labels(model, deployment_id, token_source).inc(output_tokens)
        OUTPUT_TOKENS.labels(model, deployment_id).observe(output_tokens)


def record_error(
    *, model: str | None, deployment_id: str | None, error_type: str, code: str
) -> None:
    """에러 카운터 증가.

    error_type 은 core.errors.ErrorType 열거형 값만 사용한다.
    upstream 에러 메시지 원문을 넣지 말 것.
    model 은 **등록된 논리 모델명**이거나 None 이어야 한다 (None -> "unknown").
    """
    ERRORS_TOTAL.labels(
        model or UNKNOWN_MODEL, deployment_id or NO_DEPLOYMENT, error_type, code
    ).inc()


def inflight_tracker(model: str, deployment_id: str) -> InprogressTracker:
    """진행 중 요청 수 Gauge 를 관리하는 context manager.

    Ollama 는 큐 깊이를 노출하지 않으므로, 이 값이 L3 지표의 대체재가 된다
    (docs/operations/observability-stack.md "4.3 Serving Dashboard").
    Gauge.inc/dec 는 동기 연산이라 async 코드 안에서도 일반 with 로 쓴다.
    """
    return INFLIGHT.labels(model, deployment_id).track_inprogress()


def normalize_finish_reason(value: str) -> str:
    return value if value in FINISH_REASONS else FINISH_REASON_OTHER


async def metrics_endpoint(request: Request) -> Response:
    """GET /metrics - Prometheus text exposition.

    make_asgi_app() 을 mount 하면 "/metrics" 가 "/metrics/" 로 307 리다이렉트된다.
    curl 확인이 번거로워지므로 일반 route 로 붙인다.
    """
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
