"""Gateway 에러 체계.

모든 예외는 GatewayError 로 수렴시킨다. httpx/서드파티 예외를 상위로 그대로 올리지 않는다.

- ``code``       : 사람이 문서에서 찾는 식별자 (GW-XXXX)
- ``error_type`` : **Metric label 로 쓰이는 열거형.** 자유 문자열 금지.
- ``retryable``  : Phase 6 재시도 판정의 기본값 (stream_started 여부로 상위에서 다시 판단)

명세: docs/specs/error-codes.md
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorType(StrEnum):
    """Metric label 로 노출되는 열거형. 값을 추가할 때 error-codes.md 를 먼저 갱신할 것."""

    # GW-1xxx 설정
    CONFIG_NOT_FOUND = "config_not_found"
    CONFIG_INVALID = "config_invalid"
    ADAPTER_NOT_REGISTERED = "adapter_not_registered"
    DUPLICATE_DEPLOYMENT_ID = "duplicate_deployment_id"

    # GW-4xxx 클라이언트
    INVALID_REQUEST = "invalid_request"
    MODEL_NOT_FOUND = "model_not_found"
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    NO_AVAILABLE_DEPLOYMENT = "no_available_deployment"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    REQUEST_CANCELLED = "request_cancelled"

    # GW-5xxx upstream / 내부
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_CONNECT_TIMEOUT = "upstream_connect_timeout"
    UPSTREAM_READ_TIMEOUT = "upstream_read_timeout"
    UPSTREAM_TOTAL_TIMEOUT = "upstream_total_timeout"
    UPSTREAM_PROTOCOL_ERROR = "upstream_protocol_error"
    MODEL_LOADING = "model_loading"
    OUT_OF_MEMORY = "out_of_memory"
    ALL_FALLBACKS_FAILED = "all_fallbacks_failed"
    INTERNAL_ERROR = "internal_error"


class GatewayError(Exception):
    """모든 Gateway 예외의 기반."""

    code: str = "GW-5010"
    error_type: ErrorType = ErrorType.INTERNAL_ERROR
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_error_body(self, request_id: str | None = None) -> dict[str, Any]:
        """OpenAI 호환 에러 body 로 직렬화한다.

        detail 은 로그에만 남기고 응답에는 넣지 않는다 (내부 정보 노출 방지).
        """
        return {
            "error": {
                "message": self.message,
                "type": str(self.error_type),
                "code": self.code,
                "request_id": request_id,
            }
        }


# ── GW-1xxx 설정 ────────────────────────────────────────────────
# 기동 시 발생하면 프로세스를 시작하지 않는다.
# reload 중 발생하면 기존 스냅샷을 유지한다 (Phase 4).


class ConfigError(GatewayError):
    code = "GW-1002"
    error_type = ErrorType.CONFIG_INVALID
    http_status = 500


class ConfigNotFoundError(ConfigError):
    code = "GW-1001"
    error_type = ErrorType.CONFIG_NOT_FOUND


class AdapterNotRegisteredError(ConfigError):
    code = "GW-1003"
    error_type = ErrorType.ADAPTER_NOT_REGISTERED


class DuplicateDeploymentIdError(ConfigError):
    code = "GW-1004"
    error_type = ErrorType.DUPLICATE_DEPLOYMENT_ID


# ── GW-4xxx 클라이언트 ──────────────────────────────────────────


class InvalidRequestError(GatewayError):
    code = "GW-4000"
    error_type = ErrorType.INVALID_REQUEST
    http_status = 400


class ModelNotFoundError(GatewayError):
    code = "GW-4001"
    error_type = ErrorType.MODEL_NOT_FOUND
    http_status = 404


class UnsupportedParameterError(GatewayError):
    code = "GW-4002"
    error_type = ErrorType.UNSUPPORTED_PARAMETER
    http_status = 400


class ContextLengthExceededError(GatewayError):
    code = "GW-4003"
    error_type = ErrorType.CONTEXT_LENGTH_EXCEEDED
    http_status = 400


class NoAvailableDeploymentError(GatewayError):
    code = "GW-4004"
    error_type = ErrorType.NO_AVAILABLE_DEPLOYMENT
    http_status = 503


class UnauthorizedError(GatewayError):
    code = "GW-4005"
    error_type = ErrorType.UNAUTHORIZED
    http_status = 401


class RateLimitedError(GatewayError):
    code = "GW-4006"
    error_type = ErrorType.RATE_LIMITED
    http_status = 429


class RequestCancelledError(GatewayError):
    code = "GW-4007"
    error_type = ErrorType.REQUEST_CANCELLED
    http_status = 499


# ── GW-5xxx upstream / 내부 ────────────────────────────────────


class UpstreamError(GatewayError):
    code = "GW-5001"
    error_type = ErrorType.UPSTREAM_ERROR
    http_status = 502


class UpstreamUnavailableError(UpstreamError):
    code = "GW-5002"
    error_type = ErrorType.UPSTREAM_UNAVAILABLE
    http_status = 503
    retryable = True


class UpstreamConnectTimeoutError(UpstreamError):
    code = "GW-5003"
    error_type = ErrorType.UPSTREAM_CONNECT_TIMEOUT
    http_status = 504
    retryable = True


class UpstreamReadTimeoutError(UpstreamError):
    code = "GW-5004"
    error_type = ErrorType.UPSTREAM_READ_TIMEOUT
    http_status = 504
    # 첫 토큰 전이면 재시도 가능. 상위에서 stream_started 로 다시 판단한다.
    retryable = True


class UpstreamTotalTimeoutError(UpstreamError):
    code = "GW-5005"
    error_type = ErrorType.UPSTREAM_TOTAL_TIMEOUT
    http_status = 504


class UpstreamProtocolError(UpstreamError):
    code = "GW-5006"
    error_type = ErrorType.UPSTREAM_PROTOCOL_ERROR
    http_status = 502


class ModelLoadingError(UpstreamError):
    code = "GW-5007"
    error_type = ErrorType.MODEL_LOADING
    http_status = 503
    retryable = True


class OutOfMemoryError_(UpstreamError):  # noqa: N801 - 내장 OSError 계열과 구분
    """GPU OOM. 재시도하면 상황이 더 나빠지므로 retryable=False 를 유지할 것."""

    code = "GW-5008"
    error_type = ErrorType.OUT_OF_MEMORY
    http_status = 503
    retryable = False


class AllFallbacksFailedError(GatewayError):
    code = "GW-5009"
    error_type = ErrorType.ALL_FALLBACKS_FAILED
    http_status = 502


class InternalError(GatewayError):
    code = "GW-5010"
    error_type = ErrorType.INTERNAL_ERROR
    http_status = 500


# GW-5001 upstream_error 는 upstream 상태코드가 아래에 속할 때만 재시도한다.
RETRYABLE_UPSTREAM_STATUS = frozenset({502, 503, 504})

# 상태와 무관하게 재시도하는 코드. (docs/specs/error-codes.md "재시도 판정 규칙")
ALWAYS_RETRYABLE_CODES = frozenset({"GW-5002", "GW-5003", "GW-5007"})


def is_retryable(error: GatewayError, *, stream_started: bool) -> bool:
    """재시도 가능 여부 판정 (Phase 6).

    규칙의 정본은 docs/specs/error-codes.md "재시도 판정 규칙" 이다.
    핵심: stream_started 가 True 면 무조건 False. 이미 클라이언트가 일부를 봤다.
    """
    if stream_started:
        return False
    if error.code in ALWAYS_RETRYABLE_CODES:
        return True
    if error.code == "GW-5004":
        # read timeout: 첫 토큰 전이면 재시도 가능 (stream_started 는 위에서 걸렀다)
        return True
    if error.code == "GW-5001":
        status = error.detail.get("upstream_status")
        return status in RETRYABLE_UPSTREAM_STATUS
    return False
