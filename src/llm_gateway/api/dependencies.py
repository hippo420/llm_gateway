"""FastAPI 의존성 제공자.

싱글턴(Registry, AdapterFactory)은 app.state 에 두고 여기서 꺼낸다.
모듈 전역 변수로 두면 테스트에서 격리가 안 된다.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Request

from ..adapters.factory import AdapterFactory
from ..core.context import RequestContext, new_request_id
from ..core.errors import UnauthorizedError
from ..core.logging import log_event
from ..registry.models import ModelRegistry
from ..service.chat_service import ChatService
from ..settings import Settings

log = logging.getLogger(__name__)

BEARER_PREFIX = "bearer "


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_registry(request: Request) -> ModelRegistry:
    return request.app.state.registry


def get_adapter_factory(request: Request) -> AdapterFactory:
    return request.app.state.adapters


def get_chat_service(request: Request) -> ChatService:
    """lifespan 이 만들어둔 ChatService.

    ChatService 는 상태가 없으므로 요청마다 새로 만들어도 무방하지만,
    registry/adapters 를 매번 꺼내 조립할 이유가 없어 app.state 에 둔다.
    """
    return request.app.state.chat_service


def get_request_ctx(request: Request) -> RequestContext:
    """RequestIdMiddleware 가 넣어둔 request.state.ctx 를 꺼낸다."""
    ctx: RequestContext | None = getattr(request.state, "ctx", None)
    if ctx is None:
        # 미들웨어가 빠진 조립이라는 뜻이다. 요청을 죽이지는 않되 반드시 눈에 띄게 남긴다.
        log_event(log, "request_context_missing", level=logging.WARNING, path=request.url.path)
        ctx = RequestContext(request_id=new_request_id())
        request.state.ctx = ctx
    return ctx


async def verify_api_key(request: Request) -> None:
    """API key 검사 (settings.api_key 가 비어 있으면 통과)."""
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return

    header = request.headers.get("Authorization", "")
    if not header.lower().startswith(BEARER_PREFIX):
        raise UnauthorizedError("missing bearer token")

    token = header[len(BEARER_PREFIX) :].strip()
    # 길이까지 비밀로 다루기 위해 compare_digest 로만 비교한다 (타이밍 공격 방지).
    if not hmac.compare_digest(token, settings.api_key):
        raise UnauthorizedError("invalid api key")
