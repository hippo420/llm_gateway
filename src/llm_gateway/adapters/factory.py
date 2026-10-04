"""Adapter 등록 및 인스턴스 관리.

서비스 계층은 여기서 adapter 를 받아 쓰기만 한다.
`if deployment.adapter == "ollama"` 같은 분기를 서비스에 두면 추상화가 무너진다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from ..core.errors import AdapterNotRegisteredError
from ..registry.models import ModelDeployment, RegistrySnapshot
from .base import LLMAdapter
from .ollama import OllamaAdapter
from .openai_compatible import OpenAICompatibleAdapter

# adapter 이름 -> 클래스.
# 새 adapter 를 추가하면 여기에 등록하고 docs/specs/adapter-interface.md 에 매핑 표를 쓴다.
ADAPTER_REGISTRY: dict[str, type[LLMAdapter]] = {
    OllamaAdapter.name: OllamaAdapter,
    # Phase 5: VllmAdapter.name: VllmAdapter,
    OpenAICompatibleAdapter.name: OpenAICompatibleAdapter,
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
        self._users: dict[LLMAdapter, int] = {}
        self._retired: set[LLMAdapter] = set()
        self._closing: set[asyncio.Task] = set()

    def get(self, deployment: ModelDeployment) -> LLMAdapter:
        """deployment 에 대응하는 adapter 인스턴스를 돌려준다 (없으면 생성)."""
        cached = self._instances.get(deployment.id)
        if cached is not None:
            if _connection_identity(cached.deployment) == _connection_identity(deployment):
                return cached
            del self._instances[deployment.id]
            self._retire(cached)

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

    @contextmanager
    def lease(self, deployment: ModelDeployment) -> Iterator[LLMAdapter]:
        """Keep an adapter alive across awaits/yields until the caller finishes."""
        adapter = self.get(deployment)
        self._users[adapter] = self._users.get(adapter, 0) + 1
        try:
            yield adapter
        finally:
            self._users[adapter] -= 1
            if not self._users[adapter]:
                del self._users[adapter]
                if adapter in self._retired:
                    self._schedule_close(adapter)

    def _retire(self, adapter: LLMAdapter) -> None:
        self._retired.add(adapter)
        if not self._users.get(adapter):
            self._schedule_close(adapter)

    def _schedule_close(self, adapter: LLMAdapter) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous test/setup callers have no event loop; close_all will drain these.
            return
        self._retired.discard(adapter)
        task = loop.create_task(self._close(adapter))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close(adapter: LLMAdapter) -> None:
        try:
            await adapter.aclose()
        except Exception:
            logging.getLogger(__name__).warning("retired adapter close failed")

    def retire_unused(self, snapshot: RegistrySnapshot) -> None:
        active = {d.id: d for m in snapshot.models.values() for d in m.deployments if d.enabled}
        for name, adapter in list(self._instances.items()):
            deployment = active.get(name)
            if deployment is None or _connection_identity(
                adapter.deployment
            ) != _connection_identity(deployment):
                del self._instances[name]
                self._retire(adapter)

    def register(self, name: str, adapter: LLMAdapter) -> None:
        """이미 만들어진 인스턴스를 캐시에 넣는다 (테스트에서 fake 로 바꿔치기할 때)."""
        previous = self._instances.get(name)
        if previous is not None and previous is not adapter:
            self._retire(previous)
        self._instances[name] = adapter

    async def close_all(self) -> None:
        """모든 adapter 의 aclose() 호출 후 캐시를 비운다.

        app lifespan 종료 시 반드시 부른다. 안 부르면 uvicorn 종료가 매달린다.
        """
        instances = set(self._instances.values()) | self._retired
        self._instances.clear()
        self._retired.clear()
        await asyncio.gather(*self._closing, return_exceptions=True)
        for adapter in instances:
            await self._close(adapter)


def _connection_identity(deployment: ModelDeployment) -> str:
    """캐시된 adapter 를 그대로 써도 되는지 판정하는 키."""
    return deployment.model_dump_json(exclude={"enabled", "weight", "options"})
