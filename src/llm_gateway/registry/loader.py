"""Configuration Source - 설정을 읽어 RegistrySnapshot 을 만든다.

Phase 1: YamlConfigSource 만.
Phase 4: RedisConfigSource + LayeredConfigSource(우선순위 병합) 추가.

우선순위: 요청 > Redis override > YAML base
명세: docs/specs/config-spec.md
"""

from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

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
from .overrides import OverrideDocument, TimeoutPatch, deep_merge

log = logging.getLogger(__name__)

# 지원하는 설정 스키마 버전. 포맷을 바꿀 때 여기와 config-spec.md 를 함께 올린다.
SUPPORTED_VERSIONS = frozenset({1})


SourceValue = TypeVar("SourceValue")


class ConfigSource(ABC, Generic[SourceValue]):
    """All runtime sources provide an asynchronous read boundary."""

    @abstractmethod
    async def read(self) -> SourceValue:
        """Read a candidate; consumers publish only after complete validation."""


class YamlConfigSource(ConfigSource[RegistrySnapshot]):
    """config/gateway.yaml 을 읽는다. **정본(Source of Truth).**"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._last_mtime: float | None = None
        self.raw: dict[str, Any] = {}

    async def read(self) -> RegistrySnapshot:
        return await asyncio.to_thread(self.load)

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
            # SafeLoader에 중복 키 검사만 추가한다. 임의 Python 객체 생성은 허용하지 않는다.
            mtime = self._path.stat().st_mtime_ns
            raw = yaml.load(self._path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
            if self._path.stat().st_mtime_ns != mtime:
                raise ConfigError("config changed during read; retry reload")
        except (yaml.YAMLError, OSError, UnicodeError, TypeError) as exc:
            raise ConfigError(
                "config file cannot be read as valid YAML",
                detail={"cause": type(exc).__name__},
            ) from exc

        if not isinstance(raw, dict):
            raise ConfigError(
                f"config root must be a mapping: {self._path}",
                detail={"path": str(self._path)},
            )

        try:
            validated = GatewayConfig.model_validate(raw)
            snapshot = _parse_yaml(validated.model_dump(exclude_none=True, exclude_unset=True))
        except (ValidationError, TypeError, ValueError) as exc:
            raise ConfigError("invalid configuration schema") from exc
        validate_snapshot(snapshot, known_adapters())
        self.raw = raw
        self._last_mtime = mtime
        return snapshot

    def is_stale(self) -> bool:
        """파일 mtime 이 마지막 load 시점과 다른지 (Phase 4 watcher 가 쓴다)."""
        if not self._path.is_file():
            return False
        return self._path.stat().st_mtime_ns != self._last_mtime


class RedisConfigSource(ConfigSource[OverrideDocument]):
    """운영 중 임시 override. **항상 임시다** - 영구 변경은 YAML 에 반영한다.

    Key:     gateway:config:override
    Pub/Sub: gateway:config:changed

    override 허용 필드: enabled, weight, timeout, options
    override 금지 필드: id, adapter, endpoint, upstream_model
      -> Git 에 없는 구성으로 운영되는 상태를 만들지 않기 위함.

    Writes use WATCH/MULTI so concurrent operators do not overwrite each other.
    """

    KEY = "gateway:config:override"
    CHANNEL = "gateway:config:changed"

    def __init__(self, redis_url: str, *, client: Redis | None = None) -> None:
        self.client = (
            client
            if client is not None
            else Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        )

    @staticmethod
    def decode(raw: str | bytes | None) -> OverrideDocument:
        try:
            return OverrideDocument.model_validate_json(raw) if raw else OverrideDocument()
        except ValidationError as exc:
            raise ConfigError("invalid Redis override document") from exc

    async def read(self) -> OverrideDocument:
        return self.decode(await self.client.get(self.KEY)).active()

    async def update(
        self,
        transform: Callable[[OverrideDocument], OverrideDocument],
    ) -> OverrideDocument:
        for _ in range(5):
            async with self.client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(self.KEY)
                    current = self.decode(await pipe.get(self.KEY)).active()
                    updated = transform(current)
                    pipe.multi()
                    if updated.deployments:
                        expiry = math.ceil(max(t.timestamp() for t in updated.expires_at.values()))
                        pipe.set(self.KEY, updated.model_dump_json(), exat=expiry)
                    else:
                        pipe.delete(self.KEY)
                    pipe.publish(self.CHANNEL, "changed")
                    await pipe.execute()
                    return updated
                except WatchError:
                    continue
        raise ConfigError("concurrent override updates; retry the request")

    async def close(self) -> None:
        await self.client.aclose()


class LayeredConfigSource(ConfigSource[RegistrySnapshot]):
    """Build a complete candidate. A failed read never changes accepted source views."""

    def __init__(self, base: YamlConfigSource, override: RedisConfigSource | None = None) -> None:
        self.base = base
        self.override = override
        self.base_snapshot: RegistrySnapshot | None = None
        self.override_document = OverrideDocument()
        self.redis_available: bool | None = None
        self.base_raw: dict[str, Any] = {}

    async def read(self) -> RegistrySnapshot:
        base = await self.base.read()
        document = OverrideDocument()
        if self.override:
            try:
                document = await self.override.read()
                self.redis_available = True
            except RedisError:
                self.redis_available = False
                # Startup uses YAML; once running retain last-known overrides until their TTL.
                document = self.override_document.active()
                log_event(log, "config_redis_unavailable", level=logging.WARNING)
        effective = self.merge(base, document)
        self.base_snapshot = base
        self.base_raw = self.base.raw
        self.override_document = document
        return effective

    @staticmethod
    def merge(base: RegistrySnapshot, document: OverrideDocument) -> RegistrySnapshot:
        from ..adapters.factory import known_adapters

        raw = base.model_dump()
        remaining = set(document.deployments)
        for model in raw["models"].values():
            for index, deployment in enumerate(model["deployments"]):
                patch = document.deployments.get(deployment["id"])
                if patch is not None:
                    model["deployments"][index] = deep_merge(deployment, patch.patch())
                    remaining.discard(deployment["id"])
        if remaining:
            raise ConfigError("override references an unknown deployment")
        try:
            snapshot = RegistrySnapshot.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError("invalid effective configuration") from exc
        validate_snapshot(snapshot, known_adapters())
        return snapshot


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject silently overwritten YAML keys, including duplicate model names."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.YAMLError("duplicate YAML key")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class RawDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    adapter: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    upstream_model: str = Field(min_length=1)
    enabled: bool = True
    weight: int = Field(default=100, ge=0, le=100, strict=True)
    timeout: TimeoutPatch = Field(default_factory=TimeoutPatch)
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    extra: dict[str, Any] = Field(default_factory=dict)
    api_key_env: str | None = None


class RawModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    deployments: list[RawDeployment] = Field(min_length=1)


class RawDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout: TimeoutPatch = Field(default_factory=TimeoutPatch)
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    defaults: RawDefaults = Field(default_factory=RawDefaults)
    models: dict[str, RawModel] = Field(min_length=1)
    routing: dict[str, Any] = Field(default_factory=dict)
    resilience: dict[str, Any] = Field(default_factory=dict)


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

            try:
                parsed = urlparse(dep.endpoint)
                valid_endpoint = (
                    parsed.scheme in ("http", "https")
                    and bool(parsed.hostname)
                    and (parsed.port is None or 0 < parsed.port <= 65535)
                )
            except ValueError:
                valid_endpoint = False
            if not valid_endpoint:
                raise ConfigError(
                    "invalid deployment endpoint",
                    detail={"id": dep.id},
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
                    f"invalid deployment in model {name}",
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
