"""Model Registry - 논리 모델명을 물리 deployment 로 해석한다.

이 계층이 있어서 Spring 이 endpoint/서빙 프레임워크를 모를 수 있다.
Phase 1 에서는 후보 중 첫 번째를 쓰지만, **인터페이스는 처음부터 복수 후보**를 반환한다.
그래야 Phase 5(Router)에서 시그니처를 바꾸지 않는다.

명세: docs/specs/config-spec.md
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TimeoutConfig(BaseModel):
    """3단계 timeout. 하나로 합치면 원인 구분이 불가능하다 (docs/phases/phase-06)."""

    connect: float = 5.0
    # streaming 에서는 chunk 간 무응답 허용 시간이다. 전체 시간이 아니다.
    read: float = 30.0
    total: float = 180.0

    def merged_with(self, override: "TimeoutConfig | dict[str, Any] | None") -> "TimeoutConfig":
        """TODO: 부분 override 병합. override 에 없는 필드는 self 값 유지."""
        raise NotImplementedError


class GenerationOptions(BaseModel):
    """생성 파라미터 기본값."""

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    seed: int | None = None

    def merged_with(self, override: "GenerationOptions | dict[str, Any] | None") -> "GenerationOptions":
        """TODO: 부분 override 병합.

        주의: override 의 값이 None 이면 "미지정"이므로 **덮어쓰지 않는다.**
              이걸 놓치면 요청에서 생략한 파라미터가 기본값을 지워버린다.
        """
        raise NotImplementedError


class ModelDeployment(BaseModel):
    """논리 모델의 물리적 실체 하나."""

    id: str                       # 전역 고유. 관례: <logical>@<adapter>
    logical_model: str            # 소속 논리 모델명
    adapter: str                  # factory 등록명: ollama | vllm | openai
    endpoint: str
    upstream_model: str           # 실제 서빙되는 모델명
    enabled: bool = True
    weight: int = 100             # Phase 5 부터 사용
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    # adapter 고유 옵션 (예: ollama keep_alive). 남용하면 추상화가 무너진다.
    extra: dict[str, Any] = Field(default_factory=dict)
    # 외부 API 용. 키 값 자체가 아니라 환경변수 **이름**을 담는다.
    api_key_env: str | None = None


class ModelEntry(BaseModel):
    """하나의 논리 모델과 그 후보 deployment 들."""

    name: str
    description: str | None = None
    deployments: list[ModelDeployment] = Field(default_factory=list)


class RegistrySnapshot(BaseModel):
    """불변 스냅샷.

    reload 는 이 객체를 **통째로 새로 만들어 참조만 교체**한다 (Phase 4).
    부분 갱신을 허용하면 요청 처리 도중 설정이 반쯤 바뀐 상태가 생긴다.
    """

    version: int
    loaded_at: str                       # ISO8601
    models: dict[str, ModelEntry] = Field(default_factory=dict)
    defaults_timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    defaults_options: GenerationOptions = Field(default_factory=GenerationOptions)
    # Phase 5/6 에서 사용할 원본 설정 블록
    routing: dict[str, Any] = Field(default_factory=dict)
    resilience: dict[str, Any] = Field(default_factory=dict)


class ModelRegistry:
    """스냅샷을 들고 조회를 제공한다.

    Phase 4 에서 여기에 reload / watch 가 붙는다. 조회 시그니처는 바뀌지 않아야 한다.
    """

    def __init__(self, snapshot: RegistrySnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    def swap(self, snapshot: RegistrySnapshot) -> None:
        """TODO: 원자적 교체 (Phase 4).

        검증에 성공한 스냅샷만 넣는다. 실패 시 기존 것을 유지하고 예외를 올린다.
        """
        raise NotImplementedError

    def candidates(self, logical_model: str) -> list[ModelDeployment]:
        """논리 모델의 **활성 후보 전체**를 반환한다.

        TODO: 구현.
          - 모델이 없으면 ModelNotFoundError (GW-4001)
          - enabled=False 는 제외
          - 결과가 비면 NoAvailableDeploymentError (GW-4004)
          - Phase 6: circuit breaker 가 OPEN 인 것도 여기서 제외하게 된다
        """
        raise NotImplementedError

    def resolve(self, logical_model: str) -> ModelDeployment:
        """단일 deployment 선택 (Phase 1 임시).

        TODO: 구현. candidates() 의 첫 번째를 반환한다.

        Phase 5 에서 이 메서드는 **삭제**되고 ModelRouter 가 대체한다.
        따라서 호출부를 늘리지 말 것 (ChatService 한 곳에서만 쓴다).
        """
        raise NotImplementedError

    def list_models(self) -> list[ModelEntry]:
        """TODO: GET /v1/models 용. disabled deployment 도 포함해서 보여준다."""
        raise NotImplementedError

    def all_deployments(self) -> list[ModelDeployment]:
        """TODO: readyz 헬스체크용. enabled 인 것만."""
        raise NotImplementedError
