"""Configuration Source - 설정을 읽어 RegistrySnapshot 을 만든다.

Phase 1: YamlConfigSource 만.
Phase 4: RedisConfigSource + LayeredConfigSource(우선순위 병합) 추가.

우선순위: 요청 > Redis override > YAML base
명세: docs/specs/config-spec.md
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ..core.errors import (
    AdapterNotRegisteredError,
    ConfigError,
    ConfigNotFoundError,
    DuplicateDeploymentIdError,
)
from ..core.logging import log_event
from .models import (
    GenerationOptions,
    ModelDeployment,
    ModelEntry,
    RegistrySnapshot,
    TimeoutConfig,
)

log = logging.getLogger(__name__)

# 지원하는 설정 스키마 버전. 포맷을 바꿀 때 여기와 config-spec.md 를 함께 올린다.
SUPPORTED_VERSIONS = frozenset({1})


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
        """YAML 을 읽어 검증된 스냅샷을 만든다."""
        # 순환 import 회피: factory 가 registry.models 를 import 한다.
        from ..adapters.factory import known_adapters

        if not self._path.is_file():
            raise ConfigNotFoundError(
                f"config file not found: {self._path}",
                detail={"path": str(self._path)},
            )

        try:
            # safe_load 를 쓴다. load 는 임의 파이썬 객체를 생성할 수 있어 금지.
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"config file is not valid YAML: {self._path}",
                detail={"path": str(self._path), "cause": str(exc)},
            ) from exc

        if not isinstance(raw, dict):
            raise ConfigError(
                f"config root must be a mapping: {self._path}",
                detail={"path": str(self._path)},
            )

        snapshot = _parse_yaml(raw)
        validate_snapshot(snapshot, known_adapters())
        self._last_mtime = self._path.stat().st_mtime
        return snapshot

    def is_stale(self) -> bool:
        """파일 mtime 이 마지막 load 시점과 다른지 (Phase 4 watcher 가 쓴다)."""
        if not self._path.is_file():
            return False
        return self._path.stat().st_mtime != self._last_mtime


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

    체크리스트의 정본은 docs/specs/config-spec.md "5. 검증 규칙" 이다.
    """
    if snapshot.version not in SUPPORTED_VERSIONS:
        raise ConfigError(
            f"unsupported config version: {snapshot.version}",
            detail={"version": snapshot.version, "supported": sorted(SUPPORTED_VERSIONS)},
        )

    seen: dict[str, str] = {}
    for name, entry in snapshot.models.items():
        if not entry.deployments:
            raise ConfigError(f"model {name} has no deployment", detail={"model": name})

        for dep in entry.deployments:
            if dep.id in seen:
                raise DuplicateDeploymentIdError(
                    f"duplicate deployment id: {dep.id}",
                    detail={"id": dep.id, "models": [seen[dep.id], name]},
                )
            seen[dep.id] = name

            if dep.adapter not in known_adapters:
                # 미래 Phase 를 위해 미리 적어둔 deployment(enabled=false)까지 기동을 막으면
                # 정본 설정 파일이 부팅 불가능해진다. 경고만 남기고, 실제로 트래픽을 받는
                # enabled deployment 만 GW-1003 으로 거절한다.
                if dep.enabled:
                    raise AdapterNotRegisteredError(
                        f"adapter {dep.adapter} is not registered (deployment {dep.id})",
                        detail={"adapter": dep.adapter, "known": sorted(known_adapters)},
                    )
                log_event(
                    log,
                    "config_unknown_adapter_disabled",
                    level=logging.WARNING,
                    deployment_id=dep.id,
                    adapter=dep.adapter,
                )

            parsed = urlparse(dep.endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ConfigError(
                    f"invalid endpoint {dep.endpoint} (deployment {dep.id})",
                    detail={"id": dep.id, "endpoint": dep.endpoint},
                )

            if not 0 <= dep.weight <= 100:
                raise ConfigError(
                    f"weight must be between 0 and 100 (deployment {dep.id})",
                    detail={"id": dep.id, "weight": dep.weight},
                )

            t = dep.timeout
            if t.connect > t.total or t.read > t.total:
                raise ConfigError(
                    f"timeout connect/read must be <= total (deployment {dep.id})",
                    detail={"id": dep.id, **t.model_dump()},
                )

        # 에러가 아니라 경고다. Phase 1 은 첫 후보를 쓰지만,
        # Phase 5 의 weighted routing 에서는 트래픽이 흐르지 않는 상태가 된다.
        if not sum(d.weight for d in entry.deployments if d.enabled):
            log_event(log, "config_zero_weight", level=logging.WARNING, model=name)


def _parse_yaml(raw: dict[str, Any]) -> RegistrySnapshot:
    """dict -> RegistrySnapshot 변환 (load 에서 분리해두면 테스트가 쉽다)."""
    defaults = raw.get("defaults") or {}
    defaults_timeout = TimeoutConfig().merged_with(defaults.get("timeout"))
    defaults_options = GenerationOptions().merged_with(defaults.get("options"))

    models: dict[str, ModelEntry] = {}
    for name, entry_raw in (raw.get("models") or {}).items():
        entry_raw = entry_raw or {}
        deployments: list[ModelDeployment] = []

        for dep_raw in entry_raw.get("deployments") or []:
            fields = dict(dep_raw)
            # logical_model 은 YAML 에 없다. 부모 키가 곧 값이다.
            fields["logical_model"] = name
            # deployment 는 defaults 를 부분 override 한다.
            fields["timeout"] = defaults_timeout.merged_with(fields.get("timeout"))
            fields["options"] = defaults_options.merged_with(fields.get("options"))
            try:
                deployments.append(ModelDeployment(**fields))
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"invalid deployment in model {name}: {exc}",
                    detail={"model": name, "id": fields.get("id")},
                ) from exc

        models[name] = ModelEntry(
            name=name,
            description=entry_raw.get("description"),
            deployments=deployments,
        )

    return RegistrySnapshot(
        version=raw.get("version", 1),
        loaded_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        models=models,
        defaults_timeout=defaults_timeout,
        defaults_options=defaults_options,
        routing=raw.get("routing") or {},
        resilience=raw.get("resilience") or {},
    )
