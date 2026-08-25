"""FastAPI 의존성 제공자.

싱글턴(Registry, AdapterFactory)은 app.state 에 두고 여기서 꺼낸다.
모듈 전역 변수로 두면 테스트에서 격리가 안 된다.
"""

from __future__ import annotations

from fastapi import Request

from ..adapters.factory import AdapterFactory
from ..core.context import RequestContext
from ..registry.models import ModelRegistry
from ..service.chat_service import ChatService
from ..settings import Settings


def get_settings_dep(request: Request) -> Settings:
    """TODO: request.app.state.settings 반환."""
    raise NotImplementedError


def get_registry(request: Request) -> ModelRegistry:
    """TODO: request.app.state.registry 반환."""
    raise NotImplementedError


def get_adapter_factory(request: Request) -> AdapterFactory:
    """TODO: request.app.state.adapters 반환."""
    raise NotImplementedError


def get_chat_service(request: Request) -> ChatService:
    """TODO: ChatService(registry, adapters) 반환.

    ChatService 는 상태가 없으므로 요청마다 새로 만들어도 무방하다.
    무거워지면 app.state 로 옮긴다.
    """
    raise NotImplementedError


def get_request_ctx(request: Request) -> RequestContext:
    """TODO: RequestIdMiddleware 가 넣어둔 request.state.ctx 반환.

    없으면(미들웨어 미적용 등) 즉석에서 만들어 반환하되, 경고 로그를 남긴다.
    """
    raise NotImplementedError


async def verify_api_key(request: Request) -> None:
    """API key 검사 (settings.api_key 가 비어 있으면 통과).

    TODO: 구현.
      - Authorization: Bearer <token> 파싱
      - 불일치 시 UnauthorizedError (GW-4005)
      - 비교는 hmac.compare_digest 로 (타이밍 공격 방지)
      - Spring AI 는 api-key 를 항상 보내므로, 값이 dummy 여도 헤더는 존재한다
    """
    raise NotImplementedError
