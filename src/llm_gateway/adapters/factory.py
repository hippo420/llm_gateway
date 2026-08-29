"""Adapter 등록 및 인스턴스 관리.

서비스 계층은 여기서 adapter 를 받아 쓰기만 한다.
`if deployment.adapter == "ollama"` 같은 분기를 서비스에 두면 추상화가 무너진다.
"""

from __future__ import annotations

from ..core.errors import AdapterNotRegisteredError
from ..registry.models import ModelDeployment
from .base import LLMAdapter
from .ollama import OllamaAdapter

# adapter 이름 -> 클래스.
# 새 adapter 를 추가하면 여기에 등록하고 docs/specs/adapter-interface.md 에 매핑 표를 쓴다.
ADAPTER_REGISTRY: dict[str, type[LLMAdapter]] = {
    OllamaAdapter.name: OllamaAdapter,
    # Phase 5: VllmAdapter.name: VllmAdapter,
    # 이후:    OpenAIAdapter.name: OpenAIAdapter,
}


def known_adapters() -> set[str]:
    """설정 검증(validate_snapshot)에서 사용."""
    return set(ADAPTER_REGISTRY)


class AdapterFactory:
    """deployment 당 adapter 인스턴스를 하나만 만들어 재사용한다.

    재사용이 중요한 이유: adapter 가 httpx.AsyncClient(=connection pool)를 들고 있다.
    요청마다 새로 만들면 연결이 매번 새로 맺어져 TTFT 가 나빠지고 소켓이 샌다.
    """

    def __init__(self) -> None:
        self._instances: dict[str, LLMAdapter] = {}

    def get(self, deployment: ModelDeployment) -> LLMAdapter:
        """deployment 에 대응하는 adapter 인스턴스를 돌려준다 (없으면 생성)."""
        cached = self._instances.get(deployment.id)
        if cached is not None:
            # 설정이 reload 되어 접속 정보가 바뀌면 캐시된 client 는 옛 endpoint 를 가리킨다.
            # Phase 4 의 reload 는 close_all() 을 부르지만, 여기서도 한 번 더 막는다.
            if _connection_identity(cached.deployment) == _connection_identity(deployment):
                return cached
            del self._instances[deployment.id]

        adapter_cls = ADAPTER_REGISTRY.get(deployment.adapter)
        if adapter_cls is None:
            raise AdapterNotRegisteredError(
                f"adapter {deployment.adapter} is not registered",
                detail={
                    "adapter": deployment.adapter,
                    "deployment_id": deployment.id,
                    "known": sorted(ADAPTER_REGISTRY),
                },
            )

        instance = adapter_cls(deployment)
        self._instances[deployment.id] = instance
        return instance

    def register(self, name: str, adapter: LLMAdapter) -> None:
        """이미 만들어진 인스턴스를 캐시에 넣는다 (테스트에서 fake 로 바꿔치기할 때)."""
        self._instances[name] = adapter

    async def close_all(self) -> None:
        """모든 adapter 의 aclose() 호출 후 캐시를 비운다.

        app lifespan 종료 시 반드시 부른다. 안 부르면 uvicorn 종료가 매달린다.
        """
        instances = list(self._instances.values())
        self._instances.clear()
        for adapter in instances:
            await adapter.aclose()


def _connection_identity(deployment: ModelDeployment) -> tuple[str, str, str]:
    """캐시된 adapter 를 그대로 써도 되는지 판정하는 키."""
    return (deployment.adapter, deployment.endpoint, deployment.upstream_model)
