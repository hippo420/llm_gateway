"""Adapter 등록 및 인스턴스 관리.

서비스 계층은 여기서 adapter 를 받아 쓰기만 한다.
`if deployment.adapter == "ollama"` 같은 분기를 서비스에 두면 추상화가 무너진다.
"""

from __future__ import annotations

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
        """TODO: 구현.

          - 캐시 키는 deployment.id
          - 캐시에 없으면 ADAPTER_REGISTRY 에서 클래스를 찾아 생성
          - 등록되지 않은 adapter 이름이면 AdapterNotRegisteredError (GW-1003)
          - 설정이 reload 되어 endpoint 가 바뀌면 캐시를 무효화해야 한다 (Phase 4).
            지금은 deployment 를 통째로 비교하거나, reload 시 close_all() 을 부르는 방식.
        """
        raise NotImplementedError

    async def close_all(self) -> None:
        """TODO: 모든 adapter 의 aclose() 호출 후 캐시 비우기.

        app lifespan 종료 시 반드시 부른다. 안 부르면 uvicorn 종료가 매달린다.
        """
        raise NotImplementedError
