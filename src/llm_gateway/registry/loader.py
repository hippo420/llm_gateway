"""Configuration Source - 설정을 읽어 RegistrySnapshot 을 만든다.

Phase 1: YamlConfigSource 만.
Phase 4: RedisConfigSource + LayeredConfigSource(우선순위 병합) 추가.

우선순위: 요청 > Redis override > YAML base
명세: docs/specs/config-spec.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .models import RegistrySnapshot


class ConfigSource(ABC):
    """설정 원천. Phase 4 에서 구현체가 늘어난다."""

    @abstractmethod
    def load(self) -> RegistrySnapshot:
        """설정을 읽어 검증된 스냅샷을 만든다. 실패 시 ConfigError 계열을 올린다."""

    @abstractmethod
    def is_stale(self) -> bool:
        """원천이 변경되어 reload 가 필요한지. (Phase 4 의 파일 mtime / Redis 버전 비교)"""


class YamlConfigSource(ConfigSource):
    """config/gateway.yaml 을 읽는다. **정본(Source of Truth).**"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._last_mtime: float | None = None

    def load(self) -> RegistrySnapshot:
        """TODO: 구현.

        순서:
          1. 파일 존재 확인 -> 없으면 ConfigNotFoundError (GW-1001)
          2. yaml.safe_load  (safe_load 를 쓸 것. load 는 금지)
          3. defaults 파싱
          4. models.<name>.deployments[] 를 ModelDeployment 로 변환
             - logical_model 필드를 채워 넣는다 (YAML 에는 없고 부모 키가 곧 값이다)
             - timeout/options 는 defaults 와 병합
          5. validate_snapshot() 호출
          6. RegistrySnapshot 반환
        """
        raise NotImplementedError

    def is_stale(self) -> bool:
        """TODO: 파일 mtime 이 _last_mtime 과 다른지 (Phase 4)."""
        raise NotImplementedError


class RedisConfigSource(ConfigSource):
    """운영 중 임시 override. **항상 임시다** - 영구 변경은 YAML 에 반영한다.

    Key:     gateway:config:override
    Pub/Sub: gateway:config:changed

    override 허용 필드: enabled, weight, timeout, options
    override 금지 필드: id, adapter, endpoint, upstream_model
      -> Git 에 없는 구성으로 운영되는 상태를 만들지 않기 위함.

    Phase 4 에서 구현한다.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url

    def load(self) -> RegistrySnapshot:
        raise NotImplementedError("Phase 4")

    def is_stale(self) -> bool:
        raise NotImplementedError("Phase 4")


class LayeredConfigSource(ConfigSource):
    """base 위에 override 를 얹는다. Phase 4."""

    def __init__(self, base: ConfigSource, override: ConfigSource | None = None) -> None:
        self._base = base
        self._override = override

    def load(self) -> RegistrySnapshot:
        raise NotImplementedError("Phase 4")

    def is_stale(self) -> bool:
        raise NotImplementedError("Phase 4")


def validate_snapshot(snapshot: RegistrySnapshot, known_adapters: set[str]) -> None:
    """스냅샷 검증. 실패 시 ConfigError 계열을 올린다.

    TODO: 구현. 체크리스트는 docs/specs/config-spec.md "5. 검증 규칙".
      - version 지원 범위
      - deployment id 전역 고유            -> DuplicateDeploymentIdError (GW-1004)
      - adapter 가 factory 에 등록됨        -> AdapterNotRegisteredError (GW-1003)
      - endpoint 가 유효한 URL
      - 논리 모델당 deployment >= 1
      - 0 <= weight <= 100
      - connect <= total, read <= total
      - enabled deployment 의 weight 합이 0 이면 경고 로그 (에러는 아님)
    """
    raise NotImplementedError


def _parse_yaml(raw: dict[str, Any]) -> RegistrySnapshot:
    """TODO: dict -> RegistrySnapshot 변환 (load 에서 분리해두면 테스트가 쉽다)."""
    raise NotImplementedError
