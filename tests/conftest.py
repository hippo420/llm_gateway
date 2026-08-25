"""테스트 공통 fixture.

원칙: **실제 Ollama 없이 전부 통과해야 한다.**
      LLM 이 떠 있어야만 도는 테스트는 회귀 감지에 쓸 수 없다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from llm_gateway.adapters.base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
    AdapterTimings,
    AdapterUsage,
    LLMAdapter,
)
from llm_gateway.registry.models import ModelDeployment


class FakeAdapter(LLMAdapter):
    """테스트용 어댑터.

    delay 를 주면 TTFT 측정 로직도 검증할 수 있다.
    """

    name = "fake"

    def __init__(
        self,
        deployment: ModelDeployment,
        *,
        chunks: list[str] | None = None,
        error: Exception | None = None,
        first_token_delay: float = 0.0,
    ) -> None:
        super().__init__(deployment)
        self._chunks = chunks if chunks is not None else ["안녕", "하세요"]
        self._error = error
        self._first_token_delay = first_token_delay
        self.calls: list[AdapterChatRequest] = []

    async def chat(self, request: AdapterChatRequest) -> AdapterChatResponse:
        """TODO: 구현. self.calls 에 기록하고 고정 응답 반환."""
        raise NotImplementedError

    async def stream_chat(  # type: ignore[override]
        self, request: AdapterChatRequest
    ) -> AsyncIterator[AdapterChatChunk]:
        """TODO: 구현.

          - first_token_delay 만큼 sleep 후 첫 chunk (TTFT 검증용)
          - self._chunks 를 하나씩 yield
          - 마지막에 finish_reason/usage/timings 를 담은 chunk
          - self._error 가 있으면 지정 위치에서 raise
        """
        raise NotImplementedError
        yield  # pragma: no cover

    async def health(self) -> bool:
        return True


@pytest.fixture
def deployment() -> ModelDeployment:
    """TODO: 테스트용 ModelDeployment 하나."""
    raise NotImplementedError


@pytest.fixture
def registry(deployment: ModelDeployment):
    """TODO: deployment 하나를 담은 ModelRegistry."""
    raise NotImplementedError


@pytest.fixture
def app(registry):
    """TODO: create_app() + app.state 를 테스트용으로 채운 FastAPI 앱.

    AdapterFactory 를 FakeAdapter 로 바꿔치기한다.
    """
    raise NotImplementedError


@pytest.fixture
async def client(app):
    """TODO: httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    실제 소켓을 열지 않으므로 빠르고 포트 충돌이 없다.
    """
    raise NotImplementedError


__all__ = ["FakeAdapter", "AdapterUsage", "AdapterTimings"]
