"""request_id 승계/발급 미들웨어.

Spring 이 MDC 값을 X-Request-Id 로 실어 보내면 그대로 승계한다.
이것 하나로 Spring 로그 <-> Gateway 로그를 이어붙일 수 있다.
(docs/phases/phase-02-instrumentation.md "6. Correlation")

OpenTelemetry traceparent 는 Phase 2 후반. 먼저 이것부터 확실히 동작시킨다.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ..core.context import (
    RequestContext,
    new_request_id,
    reset_request_context,
    sanitize_header_value,
    set_request_context,
)

REQUEST_ID_HEADER = "X-Request-Id"
SESSION_ID_HEADER = "X-Session-Id"
REQUEST_TYPE_HEADER = "X-Request-Type"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = (
            sanitize_header_value(request.headers.get(REQUEST_ID_HEADER)) or new_request_id()
        )

        ctx = RequestContext(
            request_id=request_id,
            session_id=sanitize_header_value(request.headers.get(SESSION_ID_HEADER)),
            user_bucket=sanitize_header_value(request.headers.get("X-User-Bucket")),
            request_type=sanitize_header_value(request.headers.get(REQUEST_TYPE_HEADER)),
        )
        # 라우터는 request.state.ctx 로, 로깅은 contextvar 로 꺼내 쓴다.
        request.state.ctx = ctx
        token = set_request_context(ctx)

        try:
            response = await call_next(request)
        finally:
            reset_request_context(token)

        # 에러 응답에도 반드시 붙어야 한다. 예외 경로는 핸들러가 응답을 만들어
        # 여기로 돌아오므로 이 한 줄로 모두 덮인다.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
