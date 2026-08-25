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

REQUEST_ID_HEADER = "X-Request-Id"
SESSION_ID_HEADER = "X-Session-Id"
REQUEST_TYPE_HEADER = "X-Request-Type"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """TODO: 구현.

          1. 헤더에서 X-Request-Id 를 읽는다. 없으면 new_request_id().
             - 외부에서 온 값이므로 길이/문자 제한을 둔다 (로그 오염·주입 방지).
               예: 128자 초과하거나 개행이 섞이면 새로 발급한다.
          2. RequestContext 를 만들어 set_request_context()
             (session_id, request_type 헤더도 함께 담는다 - Phase 5/7 에서 쓴다)
          3. request.state.ctx 에도 넣어 라우터가 쉽게 꺼내 쓰게 한다
          4. response = await call_next(request)
          5. response.headers[REQUEST_ID_HEADER] = request_id
             **에러 응답에도 반드시 붙어야 한다.** 예외 경로를 빠뜨리지 말 것.
        """
        raise NotImplementedError
