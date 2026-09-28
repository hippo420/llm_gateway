"""Model Registry - 논리 모델명을 물리 deployment 로 해석한다.

이 계층이 있어서 Spring 이 endpoint/서빙 프레임워크를 모를 수 있다.
활성 후보 목록을 반환하고, 단일 deployment 선택은 ModelRouter가 담당한다.

명세: docs/specs/config-spec.md
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ModelNotFoundError, NoAvailableDeploymentError
from ..experiment.models import Experiment, Identifier
from ..resilience.config import ResilienceConfig
from ..routing.config import RoutingConfig


def _override_fields(override: BaseModel | dict[str, Any] | None) -> dict[str, Any]:
    """병합용 override dict. **None 값(=미지정)은 걸러낸다.**"""
    if override is None:
        return {}
    raw = override.model_dump() if isinstance(override, BaseModel) else dict(override)
    return {k: v for k, v in raw.items() if v is not None}


class TimeoutConfig(BaseModel):
    """3단계 timeout. 하나로 합치면 원인 구분이 불가능하다 (docs/phases/phase-06)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    connect: float = Field(default=5.0, gt=0)
    # streaming 에서는 chunk 간 무응답 허용 시간이다. 전체 시간이 아니다.
    read: float = Field(default=30.0, gt=0)
    total: float = Field(default=180.0, gt=0)

    def merged_with(self, override: TimeoutConfig | dict[str, Any] | None) -> TimeoutConfig:
        """부분 override 병합. override 에 없는 필드는 self 값을 유지한다."""
        return TimeoutConfig(**{**self.model_dump(), **_override_fields(override)})


class GenerationOptions(BaseModel):
    """생성 파라미터 기본값."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    stop: list[str] | None = None
    seed: int | None = None
    # Ollama num_ctx. 모델별 기본값을 gateway.yaml 에 두고 요청이 덮어쓴다.
    num_ctx: int | None = Field(default=None, gt=0)

    def merged_with(self, override: GenerationOptions | dict[str, Any] | None) -> GenerationOptions:
        """부분 override 병합.

        override 의 값이 None 이면 "미지정"이므로 **덮어쓰지 않는다.**
        이걸 놓치면 요청에서 생략한 파라미터가 기본값을 지워버린다.
        """
        return GenerationOptions(**{**self.model_dump(), **_override_fields(override)})


class ModelDeployment(BaseModel):
    """논리 모델의 물리적 실체 하나."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)  # 전역 고유. 관례: <logical>@<adapter>
    logical_model: str  # 소속 논리 모델명
    adapter: str  # factory 등록명: ollama | vllm | openai
    endpoint: str
    upstream_model: str  # 실제 서빙되는 모델명
    enabled: bool = True
    weight: int = Field(default=100, ge=0, le=100)  # weighted 전략의 상대 가중치
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    # adapter 고유 옵션 (예: ollama keep_alive). 남용하면 추상화가 무너진다.
    extra: dict[str, Any] = Field(default_factory=dict)
    # 외부 API 용. 키 값 자체가 아니라 환경변수 **이름**을 담는다.
    api_key_env: str | None = None


class ModelEntry(BaseModel):
    """하나의 논리 모델과 그 후보 deployment 들."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str | None = None
    deployments: list[ModelDeployment] = Field(default_factory=list)


class RegistrySnapshot(BaseModel):
    """불변 스냅샷.

    reload 는 이 객체를 **통째로 새로 만들어 참조만 교체**한다 (Phase 4).
    부분 갱신을 허용하면 요청 처리 도중 설정이 반쯤 바뀐 상태가 생긴다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    loaded_at: str  # ISO8601
    models: dict[str, ModelEntry] = Field(default_factory=dict)
    defaults_timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    defaults_options: GenerationOptions = Field(default_factory=GenerationOptions)
    # 요청 선택과 실행 정책을 같은 스냅샷에 담는다.
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    experiments: dict[Identifier, Experiment] = Field(default_factory=dict, max_length=20)


class ModelRegistry:
    """스냅샷을 들고 조회를 제공한다.

    ConfigManager가 검증된 스냅샷을 교체하고 ModelRouter가 후보를 선택한다.
    """

    def __init__(self, snapshot: RegistrySnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    def swap(self, snapshot: RegistrySnapshot) -> None:
        """검증된 스냅샷으로 원자적 교체 (Phase 4 에서 reload 가 사용한다).

        참조 하나만 바꾼다. 처리 중인 요청은 이미 꺼내 쓴 옛 스냅샷을 끝까지 쓴다.
        """
        self._snapshot = snapshot

    def candidates(
        self,
        logical_model: str,
        *,
        snapshot: RegistrySnapshot | None = None,
    ) -> list[ModelDeployment]:
        """논리 모델의 **활성 후보 전체**를 반환한다.

        Phase 6 에서는 circuit breaker 가 OPEN 인 것도 여기서 제외하게 된다.
        """
        entry = (snapshot or self._snapshot).models.get(logical_model)
        if entry is None:
            raise ModelNotFoundError(
                f"model '{logical_model}' is not registered",
                detail={"model": logical_model},
            )

        enabled = [d for d in entry.deployments if d.enabled]
        if not enabled:
            raise NoAvailableDeploymentError(
                f"no available deployment for model '{logical_model}'",
                detail={"model": logical_model, "total": len(entry.deployments)},
            )
        return enabled

    def list_models(self) -> list[ModelEntry]:
        """GET /v1/models 용. disabled deployment 도 포함해서 보여준다."""
        return list(self._snapshot.models.values())

    def all_deployments(self) -> list[ModelDeployment]:
        """readyz 헬스체크용. enabled 인 것만."""
        return [
            d for entry in self._snapshot.models.values() for d in entry.deployments if d.enabled
        ]
