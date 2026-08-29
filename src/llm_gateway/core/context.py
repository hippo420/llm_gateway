"""요청 단위 컨텍스트.

request_id 를 contextvar 로 들고 다녀서, 로그/에러 응답 어디서든 인자 전달 없이 꺼내 쓴다.

주의: request_id 는 **로그와 응답 헤더 전용**이다.
      Metric label 로 절대 쓰지 않는다 (cardinality 폭발).
      -> docs/specs/metrics-spec.md "금지 Label"
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

_request_context: ContextVar[RequestContext | None] = ContextVar(
    "llm_gateway_request_context", default=None
)


@dataclass
class RequestContext:
    """하나의 HTTP 요청에 대한 상관 정보.

    Phase 진행에 따라 필드가 추가된다:
      Phase 5  routing_decision
      Phase 6  attempt, stream_started, fallback_from
      Phase 7  experiment, variant
    """

    request_id: str
    # sticky routing / A/B bucketing 키 (Phase 5, 7)
    session_id: str | None = None
    # 라우팅 힌트 (Phase 5): simple_qa | report_analysis | news_summary ...
    request_type: str | None = None
    # 논리 모델명
    model: str | None = None
    # 실제 선택된 deployment id
    deployment_id: str | None = None
    # 첫 content chunk 를 이미 내보냈는가 -> 재시도/폴백 차단 플래그 (Phase 6)
    stream_started: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    """contextvar 에 바인딩하고 reset 용 Token 을 돌려준다.

    ASGI 는 요청마다 별도 task 에서 실행되므로 보통 reset 없이도 누수되지 않지만,
    BackgroundTask 를 쓰기 시작하면 달라진다. 미들웨어가 finally 에서 reset 한다.
    """
    return _request_context.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    """set_request_context() 가 돌려준 Token 으로 이전 값을 복원한다."""
    _request_context.reset(token)


def get_request_context() -> RequestContext | None:
    """현재 컨텍스트 반환 (없으면 None)."""
    return _request_context.get()


def current_request_id() -> str | None:
    """편의 함수. 로깅 필터에서 사용."""
    ctx = _request_context.get()
    return ctx.request_id if ctx is not None else None
