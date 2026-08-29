"""구조화 로깅.

JSON 한 줄 = 한 이벤트. Kibana/Loki 에서 request_id 로 Spring 로그와 이어붙이는 것이 목적이다.

요청당 요약 로그 예시는 docs/operations/observability-stack.md "5. 로그" 참고.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..settings import Settings
from .context import current_request_id

# LogRecord 의 표준 속성. JsonFormatter 가 extra 필드만 골라내는 데 쓴다.
_STANDARD_RECORD_KEYS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName", "request_id", "event"}

TEXT_FORMAT = "%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    """모든 LogRecord 에 request_id 를 주입한다.

    컨텍스트가 없으면 "-" 로 채운다 (포맷터가 KeyError 나지 않도록).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """LogRecord -> JSON 한 줄.

    고정 필드: ts, level, logger, event, message, request_id
    추가 필드: record 에 붙은 extra dict 를 그대로 펼친다.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # 순환 참조/비직렬화 객체는 문자열로 떨어뜨려 로깅이 죽지 않게 한다.
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """루트 로거 설정. 프로세스당 한 번, 기동 시 부른다."""
    formatter: logging.Formatter = (
        JsonFormatter() if settings.log_format == "json" else logging.Formatter(TEXT_FORMAT)
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    # 중복 핸들러가 붙지 않도록 기존 핸들러를 걷어낸다 (reload / 테스트 재호출 대비).
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # uvicorn 기본 access log 는 request_id 를 모른다. AccessLogMiddleware 로 대체한다.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def log_event(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    """구조화 이벤트 로깅 헬퍼.

    사용 예:
        log_event(log, "chat_completed", model="qwen-7b", ttft_sec=0.83, ...)

    금지: 프롬프트/응답 원문을 fields 에 넣지 말 것.
          (settings.log_prompt=True 인 개발 환경에서만 별도 이벤트로 남긴다)
    """
    level = fields.pop("level", logging.INFO)
    logger.log(level, event, extra={"event": event, **fields})
