"""접근 로그 미들웨어.

uvicorn 기본 access log 는 request_id 를 모르므로 비활성화하고 이것으로 대체한다.
(core.logging.configure_logging 에서 uvicorn.access 를 끈다)

여기서 남기는 것은 HTTP 수준 요약이다.
LLM 요약(토큰/TTFT/TPS)은 ChatService 가 "chat_completed" 이벤트로 따로 남긴다.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class AccessLogMiddleware(BaseHTTPMiddleware):
    # 헬스체크/메트릭은 시끄럽기만 하므로 제외한다
    SKIP_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """TODO: 구현.

          - perf_counter 로 처리 시간 측정
          - SKIP_PATHS 는 로그를 남기지 않는다
          - log_event(log, "http_request", method=..., path=..., status=...,
                      duration_ms=..., client=...)
          - 예외가 나도 로그는 남긴다 (try/finally)

        금지: query string 이나 body 를 통째로 남기지 말 것.
              프롬프트가 섞여 들어온다.
        """
        raise NotImplementedError
