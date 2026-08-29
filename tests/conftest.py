"""테스트 공통 fixture.

원칙: **실제 Ollama 없이 전부 통과해야 한다.**
      LLM 이 떠 있어야만 도는 테스트는 회귀 감지에 쓸 수 없다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from llm_gateway.adapters.base import (
    AdapterChatChunk,
    AdapterChatRequest,
    AdapterChatResponse,
    AdapterTimings,
    AdapterUsage,
    LLMAdapter,
)
from llm_gateway.main import create_app
from llm_gateway.registry.models import (
    GenerationOptions,
    ModelDeployment,
    ModelEntry,
    ModelRegistry,
    RegistrySnapshot,
    TimeoutConfig,
)
from llm_gateway.settings import Settings

# FakeAdapter 가 마지막 chunk 에 싣는 값. Ollama 응답 샘플과 같은 모양이다.
FAKE_USAGE = AdapterUsage(input_tokens=12, output_tokens=5, source="upstream")
FAKE_TIMINGS = AdapterTimings(
    queue_sec=None,
    prompt_eval_sec=0.13,
    generation_sec=0.42,
    load_sec=0.01,
)


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
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return AdapterChatResponse(
            content="".join(self._chunks),
            finish_reason="stop",
            usage=FAKE_USAGE,
            timings=FAKE_TIMINGS,
            upstream_model=request.model,
        )

    async def stream_chat(  # type: ignore[override]
        self, request: AdapterChatRequest
    ) -> AsyncIterator[AdapterChatChunk]:
        self.calls.append(request)

        if self._first_token_delay:
            await asyncio.sleep(self._first_token_delay)

        for index, text in enumerate(self._chunks):
            # error 는 첫 chunk 를 내보낸 뒤에 터뜨린다.
            # 스트림이 이미 시작된 상태의 에러 처리를 검증하기 위함이다.
            if self._error is not None and index == 1:
                raise self._error
            yield AdapterChatChunk(delta=text)

        if self._error is not None and len(self._chunks) < 2:
            raise self._error

        yield AdapterChatChunk(
            delta="",
            finish_reason="stop",
            usage=FAKE_USAGE,
            timings=FAKE_TIMINGS,
        )

    async def health(self) -> bool:
        return True


@pytest.fixture
def deployment() -> ModelDeployment:
    """테스트용 ModelDeployment 하나. 기본 옵션이 적용됐는지 볼 수 있게 값을 채워둔다."""
    return ModelDeployment(
        id="qwen-7b@fake",
        logical_model="qwen-7b",
        adapter="fake",
        endpoint="http://localhost:11434",
        upstream_model="qwen2.5:7b",
        enabled=True,
        weight=100,
        timeout=TimeoutConfig(connect=1, read=5, total=10),
        options=GenerationOptions(temperature=0.2, top_p=0.9, max_tokens=2048),
        extra={"keep_alive": "30m"},
    )


@pytest.fixture
def registry(deployment: ModelDeployment) -> ModelRegistry:
    """deployment 하나를 담은 ModelRegistry."""
    return ModelRegistry(
        RegistrySnapshot(
            version=1,
            loaded_at="2026-08-25T10:00:00+00:00",
            models={
                "qwen-7b": ModelEntry(
                    name="qwen-7b",
                    description="test model",
                    deployments=[deployment],
                ),
                # 후보가 전부 disabled 인 경우(GW-4004)를 만들기 위한 모델
                "qwen-off": ModelEntry(
                    name="qwen-off",
                    deployments=[
                        deployment.model_copy(
                            update={
                                "id": "qwen-off@fake",
                                "logical_model": "qwen-off",
                                "enabled": False,
                            }
                        )
                    ],
                ),
            },
        )
    )


@pytest.fixture
def fake_adapter(deployment: ModelDeployment) -> FakeAdapter:
    return FakeAdapter(deployment)


@pytest.fixture
def app(registry: ModelRegistry, deployment: ModelDeployment, fake_adapter: FakeAdapter):
    """create_app() + app.state 를 테스트용으로 채운 FastAPI 앱.

    lifespan 을 돌리지 않고 app.state 를 직접 채운다.
    실제 gateway.yaml 과 Ollama 에 의존하지 않게 하기 위함이다.
    """
    from llm_gateway.adapters.factory import AdapterFactory
    from llm_gateway.service.chat_service import ChatService

    settings = Settings(api_key="", log_format="text")
    application = create_app(settings)

    adapters = AdapterFactory()
    # deployment.id 로 캐시해두면 factory 가 ollama 를 새로 만들지 않는다.
    adapters.register(deployment.id, fake_adapter)

    application.state.registry = registry
    application.state.adapters = adapters
    application.state.chat_service = ChatService(registry, adapters)
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    """실제 소켓을 열지 않으므로 빠르고 포트 충돌이 없다."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


__all__ = ["AdapterTimings", "AdapterUsage", "FakeAdapter"]
