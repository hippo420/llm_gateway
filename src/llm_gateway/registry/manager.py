"""Serialize configuration changes; validated snapshots are published without awaits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter, Gauge
from redis.exceptions import RedisError

from ..adapters.factory import AdapterFactory
from ..core.errors import ConfigError, InvalidRequestError, ModelNotFoundError
from ..core.logging import log_event
from .loader import LayeredConfigSource
from .models import ModelRegistry, RegistrySnapshot
from .overrides import DeploymentOverride, OverrideDocument

log = logging.getLogger(__name__)
CONFIG_RELOAD = Counter(
    "llm_gateway_config_reload_total",
    "Configuration reload attempts.",
    ["source", "result"],
)
CONFIG_VERSION = Gauge(
    "llm_gateway_config_version", "Applied process-local configuration revision."
)
CONFIG_TIMESTAMP = Gauge(
    "llm_gateway_config_version_timestamp",
    "Unix timestamp of last effective config change.",
)
DEPLOYMENT_ENABLED = Gauge(
    "llm_gateway_deployment_enabled",
    "Effective deployment enabled flag.",
    ["deployment_id"],
)
DEPLOYMENT_WEIGHT = Gauge(
    "llm_gateway_deployment_weight",
    "Effective deployment routing weight.",
    ["deployment_id"],
)


def fingerprint(snapshot: RegistrySnapshot) -> str:
    data = snapshot.model_dump(exclude={"loaded_at"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def redact(value: Any) -> Any:
    """Mask secret fields recursively, and credentials/query strings embedded in URLs."""
    from urllib.parse import urlsplit, urlunsplit

    def secret_key(key: Any) -> bool:
        normalized = str(key).lower().replace("_", "").replace("-", "")
        return normalized in {"key", "token"} or any(
            part in normalized
            for part in (
                "apikey",
                "password",
                "secret",
                "authorization",
                "credential",
                "accesstoken",
                "refreshtoken",
                "authtoken",
                "bearer",
                "cookie",
                "privatekey",
            )
        )

    if isinstance(value, dict):
        return {key: "***" if secret_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
            host = parsed.netloc.rsplit("@", 1)[-1]
            if "@" in parsed.netloc:
                host = "***@" + host
            return urlunsplit(
                (
                    parsed.scheme,
                    host,
                    parsed.path,
                    "***" if parsed.query else "",
                    "***" if parsed.fragment else "",
                )
            )
        except ValueError:
            return "***"
    return value


class ConfigManager:
    def __init__(
        self,
        source: LayeredConfigSource,
        registry: ModelRegistry,
        adapters: AdapterFactory,
    ) -> None:
        self.source = source
        self.registry = registry
        self.adapters = adapters
        self.lock = asyncio.Lock()
        self.revision = 1
        self.content_hash = fingerprint(registry.snapshot)
        self.last_error: str | None = None
        self.tasks: list[asyncio.Task] = []
        CONFIG_VERSION.set(self.revision)
        CONFIG_TIMESTAMP.set(datetime.now(UTC).timestamp())
        self._metric_deployments: set[str] = set()
        self.update_deployment_metrics(registry.snapshot)

    def update_deployment_metrics(self, snapshot: RegistrySnapshot) -> None:
        deployments = {d.id: d for model in snapshot.models.values() for d in model.deployments}
        for removed in self._metric_deployments - deployments.keys():
            DEPLOYMENT_ENABLED.remove(removed)
            DEPLOYMENT_WEIGHT.remove(removed)
        for name, deployment in deployments.items():
            DEPLOYMENT_ENABLED.labels(name).set(int(deployment.enabled))
            DEPLOYMENT_WEIGHT.labels(name).set(deployment.weight)
        self._metric_deployments = set(deployments)

    def apply(self, candidate: RegistrySnapshot, trigger: str) -> bool:
        content_hash = fingerprint(candidate)
        changed = content_hash != self.content_hash
        if changed:
            self.registry.swap(candidate)
            self.content_hash = content_hash
            self.revision += 1
            self.adapters.retire_unused(candidate)
            self.update_deployment_metrics(candidate)
            CONFIG_VERSION.set(self.revision)
            CONFIG_TIMESTAMP.set(datetime.now(UTC).timestamp())
            log_event(log, "config_applied", source=trigger, revision=self.revision)
        self.last_error = None
        CONFIG_RELOAD.labels(trigger, "success").inc()
        return changed

    async def reload(self, trigger: str = "admin", *, refresh_base: bool = True) -> bool:
        async with self.lock:
            try:
                previous = self.source.override_document
                candidate = await self.source.read(refresh_base=refresh_base)
                if previous != self.source.override_document:
                    document = self.source.override_document
                    log_event(
                        log,
                        "config_override_loaded",
                        source=trigger,
                        actor=document.updated_by,
                        reason=document.reason,
                        updated_at=document.updated_at,
                        before=redact(previous.model_dump(mode="json")),
                        after=redact(document.model_dump(mode="json")),
                    )
                return self.apply(candidate, trigger)
            except Exception as exc:
                self.last_error = type(exc).__name__
                CONFIG_RELOAD.labels(trigger, "failed").inc()
                log_event(
                    log,
                    "config_reload_failed",
                    level=logging.ERROR,
                    source=trigger,
                    error_type=self.last_error,
                )
                raise ConfigError("configuration reload failed; previous config retained") from exc

    async def change_override(
        self,
        deployment_id: str,
        patch: DeploymentOverride | None,
        *,
        actor: str,
        reason: str,
        ttl_sec: int = 3600,
    ) -> None:
        store = self.source.override
        if store is None:
            raise ConfigError("Redis override store is not configured")
        async with self.lock:
            base = self.source.base_snapshot
            if base is None:
                raise ConfigError("base configuration is not loaded")
            ids = {dep.id for model in base.models.values() for dep in model.deployments}
            if deployment_id not in ids:
                raise ModelNotFoundError("deployment not found")
            before: dict[str, Any] = {}
            candidate = self.registry.snapshot

            def transform(current: OverrideDocument) -> OverrideDocument:
                nonlocal before, candidate
                now = datetime.now(UTC)
                deployments = dict(current.deployments)
                expirations = dict(current.expires_at)
                previous = deployments.get(deployment_id)
                before = previous.patch() if previous else {}
                if patch is None:
                    deployments.pop(deployment_id, None)
                    expirations.pop(deployment_id, None)
                else:
                    # PUT replaces this deployment's override; other deployments retain their TTL.
                    deployments[deployment_id] = patch
                    expirations[deployment_id] = now + timedelta(seconds=ttl_sec)
                updated = OverrideDocument(
                    updated_at=now,
                    updated_by=actor,
                    reason=reason,
                    deployments=deployments,
                    expires_at=expirations,
                )
                try:
                    candidate = self.source.merge(base, updated)
                except ConfigError as exc:
                    raise InvalidRequestError("override produces invalid effective config") from exc
                return updated

            try:
                updated = await store.update(transform)
            except RedisError as exc:
                CONFIG_RELOAD.labels("override", "failed").inc()
                raise ConfigError(
                    "Redis override update unavailable; retry after recovery"
                ) from exc
            except (ConfigError, InvalidRequestError):
                CONFIG_RELOAD.labels("override", "failed").inc()
                raise
            # No await between the accepted source update and the registry pointer swap.
            self.source.override_document = updated
            self.source.redis_available = True
            self.apply(candidate, "override")
            log_event(
                log,
                "config_override_changed",
                actor=actor,
                reason=reason,
                changed_at=datetime.now(UTC).isoformat(),
                deployment_id=deployment_id,
                before=redact(before),
                after=redact(patch.patch() if patch else {}),
                expires_at=updated.expires_at.get(deployment_id),
            )

    def effective(self) -> dict[str, Any]:
        return redact(
            {
                "revision": self.revision,
                "content_hash": self.content_hash,
                "config": self.registry.snapshot.model_dump(),
                "override_expires_at": self.source.override_document.model_dump(mode="json")[
                    "expires_at"
                ],
                "redis_available": self.source.redis_available,
                "last_reload_error": self.last_error,
            }
        )

    async def atomic_override_with_state(
        self,
        state_key: str,
        transform: Callable[
            [str | bytes | None, OverrideDocument, RegistrySnapshot], tuple[str, OverrideDocument]
        ],
        audit_events: Callable[[], list[dict]] | None = None,
    ) -> str:
        """Policy extension of Phase 4: validate first, commit state + patches together."""
        store = self.source.override
        if store is None:
            raise ConfigError("policies require a Redis override store")
        async with self.lock:
            base = self.source.base_snapshot
            if base is None:
                raise ConfigError("base configuration is not loaded")
            candidate = self.registry.snapshot

            def update(
                raw: str | bytes | None, current: OverrideDocument
            ) -> tuple[str, OverrideDocument]:
                nonlocal candidate
                state, document = transform(raw, current, base)
                candidate = self.source.merge(base, document)
                return state, document

            try:
                state, document = await store.update_with_state(state_key, update, audit_events)
            except RedisError as exc:
                raise ConfigError(
                    "policy transaction unavailable; inspect history before retry"
                ) from exc
            self.source.override_document = document
            self.source.redis_available = True
            self.apply(candidate, "policy")
            return state

    def sources(self) -> dict[str, Any]:
        return redact(
            {
                "base": self.source.base_raw,
                "override": self.source.override_document.model_dump(mode="json"),
                "note": "Last accepted source layers; pending and rejected changes excluded.",
            }
        )

    def start(self, file_interval: float, redis_interval: float = 5) -> None:
        if file_interval > 0 or self.source.override is not None:
            self.tasks.append(
                asyncio.create_task(
                    self.poll(file_interval, redis_interval),
                    name="config-poll",
                )
            )
        if self.source.override is not None:
            self.tasks.append(asyncio.create_task(self.subscribe(), name="config-pubsub"))

    async def poll(self, file_interval: float, redis_interval: float) -> None:
        loop = asyncio.get_running_loop()
        next_file = loop.time() + file_interval if file_interval > 0 else float("inf")
        next_redis = (
            loop.time() + redis_interval if self.source.override is not None else float("inf")
        )
        while True:
            await asyncio.sleep(max(0, min(next_file, next_redis) - loop.time()))
            if loop.time() >= next_file:
                try:
                    if await asyncio.to_thread(self.source.base.is_stale):
                        await self.reload("poll")
                except ConfigError:
                    pass  # reload records the failure; Redis reconciliation must still run.
                except OSError as exc:
                    log_event(
                        log,
                        "config_poll_failed",
                        level=logging.WARNING,
                        error_type=type(exc).__name__,
                    )
                next_file = loop.time() + file_interval
            if loop.time() >= next_redis:
                try:
                    await self.reload("poll", refresh_base=False)
                except ConfigError:
                    pass
                next_redis = loop.time() + redis_interval

    async def subscribe(self) -> None:
        store = self.source.override
        assert store is not None
        while True:
            try:
                async with store.client.pubsub() as pubsub:
                    await pubsub.subscribe(store.CHANNEL)
                    # Covers a change before subscription acknowledgement / during reconnect.
                    await self.reload("pubsub", refresh_base=False)
                    async for message in pubsub.listen():
                        if message["type"] == "message":
                            try:
                                await self.reload("pubsub", refresh_base=False)
                            except ConfigError:
                                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    log,
                    "config_pubsub_disconnected",
                    level=logging.WARNING,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(5)

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.source.override:
            await self.source.override.close()
