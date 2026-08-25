"""구조화 로깅.

JSON 한 줄 = 한 이벤트. Kibana/Loki 에서 request_id 로 Spring 로그와 이어붙이는 것이 목적이다.

요청당 요약 로그 예시는 docs/operations/observability-stack.md "5. 로그" 참고.
"""

from __future__ import annotations

import logging
from typing import Any

from ..settings import Settings


class RequestIdFilter(logging.Filter):
    """모든 LogRecord 에 request_id 를 주입한다.

    TODO: core.context.current_request_id() 를 읽어 record.request_id 에 넣는다.
          컨텍스트가 없으면 "-" 로 채운다(포맷터가 KeyError 나지 않도록).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        raise NotImplementedError


class JsonFormatter(logging.Formatter):
    """LogRecord -> JSON 한 줄.

    고정 필드: ts, level, logger, event, message, request_id
    추가 필드: record 에 붙은 extra dict 를 그대로 펼친다.
    """

    def format(self, record: logging.LogRecord) -> str:
        """TODO: 구현.

        주의:
          - ts 는 ISO8601 UTC (밀리초까지).
          - exc_info 가 있으면 stacktrace 를 문자열 필드로.
          - 순환 참조/비직렬화 객체는 default=str 로 방어.
        """
        raise NotImplementedError


def configure_logging(settings: Settings) -> None:
    """루트 로거 설정.

    TODO: 구현.
      - settings.log_format == "json" 이면 JsonFormatter, 아니면 사람이 읽는 포맷
      - RequestIdFilter 부착
      - uvicorn.access 로거는 비활성화하고 AccessLogMiddleware 로 대체
        (uvicorn 기본 access log 는 request_id 를 모른다)
      - 중복 핸들러가 붙지 않도록 기존 핸들러 제거 후 등록
    """
    raise NotImplementedError


def log_event(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    """구조화 이벤트 로깅 헬퍼.

    사용 예:
        log_event(log, "chat_completed", model="qwen-7b", ttft_sec=0.83, ...)

    TODO: 구현. logger.info(event, extra={"event": event, **fields})

    금지: 프롬프트/응답 원문을 fields 에 넣지 말 것.
          (settings.log_prompt=True 인 개발 환경에서만 별도 이벤트로 남긴다)
    """
    raise NotImplementedError
