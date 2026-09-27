"""Phase 4: real redis-py transactions via fakeredis, without Redis/Ollama services."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from llm_gateway.adapters.factory import ADAPTER_REGISTRY, AdapterFactory
from llm_gateway.core.context import RequestContext
from llm_gateway.core.errors import ConfigError
from llm_gateway.main import create_app
from llm_gateway.registry.loader import LayeredConfigSource, RedisConfigSource, YamlConfigSource
from llm_gateway.registry.manager import CONFIG_RELOAD, ConfigManager, redact
from llm_gateway.registry.models import ModelRegistry
from llm_gateway.registry.overrides import DeploymentOverride, OverrideDocument, deep_merge
from llm_gateway.schemas.chat import ChatCompletionRequest
from llm_gateway.service.chat_service import ChatService
from llm_gateway.settings import Settings

from .conftest import FakeAdapter
from .test_registry import SAMPLE

DEPLOYMENT = "qwen-7b@ollama"
BODY = {"model": "qwen-7b", "messages": [{"role": "user", "content": "hello"}]}


def observe_reloads(manager, predicate):
    event = asyncio.Event()
    original = manager.reload

    async def reload(trigger="admin", **kwargs):
        try:
            return await original(trigger, **kwargs)
        finally:
            if predicate():
                event.set()

    manager.reload = reload
    return event


@pytest.fixture
async def system(tmp_path, monkeypatch):
    path = tmp_path / "gateway.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisConfigSource("redis://unused", client=redis)
    source = LayeredConfigSource(YamlConfigSource(path), store)
    registry = ModelRegistry(await source.read())
    factory = AdapterFactory()
    monkeypatch.setitem(ADAPTER_REGISTRY, "ollama", FakeAdapter)
    manager = ConfigManager(source, registry, factory)
    app = create_app(Settings(api_key="admin-secret", config_path=path, config_reload_sec=0))
    app.state.registry = registry
    app.state.adapters = factory
    app.state.chat_service = ChatService(registry, factory)
    app.state.config_manager = manager
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer admin-secret"},
    ) as client:
        yield manager, path, redis, app, client
    await manager.close()
    await factory.close_all()


@pytest.mark.parametrize(
    "text",
    [
        "models: [",
        "models: []",
        "models: {}",
        "defaults: []\nmodels: {}",
        SAMPLE.replace("endpoint:", "endpont:"),
        SAMPLE.replace("read: 60", "read: -1"),
        SAMPLE.replace("read: 60", "read: 9999"),
        SAMPLE.replace("read: 60", "read: .nan"),
        SAMPLE.replace("weight: 100", "weight: 101"),
        SAMPLE.replace("temperature: 0.5", "temperature: 9"),
        SAMPLE.replace("http://localhost:11434", "http://localhost:bad"),
        SAMPLE.replace("version: 1", "version: 999"),
        SAMPLE + "\nunknown_field: true\n",
        SAMPLE + "\nversion: 1\n",
    ],
)
async def test_invalid_yaml_preserves_exact_snapshot(system, text):
    manager, path, _, _, client = system
    old = manager.registry.snapshot
    old_sources = manager.sources()
    failed = CONFIG_RELOAD.labels("admin", "failed")._value.get()
    path.write_text(text, encoding="utf-8")
    response = await client.post("/admin/config/reload")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "GW-1002"
    assert manager.registry.snapshot is old
    assert manager.sources() == old_sources
    assert CONFIG_RELOAD.labels("admin", "failed")._value.get() == failed + 1
    assert (await client.post("/v1/chat/completions", json=BODY)).status_code == 200


def test_deep_merge_preserves_base_lists_and_none():
    base = {"timeout": {"connect": 5, "read": 60}, "stop": ["old"], "enabled": True}
    override = {"timeout": {"read": 120}, "stop": ["new"], "enabled": None}
    merged = deep_merge(base, override)
    assert merged == {"timeout": {"connect": 5, "read": 120}, "stop": ["new"], "enabled": True}
    merged["stop"].append("x")
    assert base["stop"] == ["old"]
    assert override["stop"] == ["new"]


async def test_override_immediately_disables_then_delete_restores(system, caplog):
    manager, _, redis, _, client = system
    caplog.set_level(logging.INFO)
    response = await client.put(
        f"/admin/deployments/{DEPLOYMENT}",
        json={
            "enabled": False,
            "ttl_sec": 60,
            "reason": "maintenance",
        },
    )
    assert response.status_code == 200
    assert (await client.post("/v1/chat/completions", json=BODY)).status_code == 503
    assert 0 < await redis.ttl(RedisConfigSource.KEY) <= 61
    assert manager.source.base_snapshot.models["qwen-7b"].deployments[0].enabled is True
    records = [r for r in caplog.records if getattr(r, "event", "") == "config_override_changed"]
    assert records[0].actor.startswith("api-key:")
    assert records[0].reason == "maintenance"
    assert records[0].before == {}
    assert records[0].after == {"enabled": False}
    assert "admin-secret" not in caplog.text
    response = await client.delete(
        f"/admin/deployments/{DEPLOYMENT}/override",
        params={
            "reason": "maintenance complete",
        },
    )
    assert response.status_code == 200
    assert await redis.get(RedisConfigSource.KEY) is None
    assert (await client.post("/v1/chat/completions", json=BODY)).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"endpoint": "http://other"},
        {"adapter": "vllm"},
        {"upstream_model": "other"},
        {"id": "other"},
        {"weight": -1},
        {"weight": 101},
        {"ttl_sec": 0, "weight": 50},
        {"ttl_sec": 86401, "weight": 50},
        {"timeout": {"typo": 1}},
        {"timeout": {"read": 9999}},
        {"options": {"top_p": 2}},
        {"enabled": None},
        {"options": {}},
        {"weight": 50, "reason": " "},
    ],
)
async def test_invalid_override_does_not_write_or_swap(system, body):
    manager, _, redis, _, client = system
    old = manager.registry.snapshot
    response = await client.put(
        f"/admin/deployments/{DEPLOYMENT}",
        json={
            "reason": "testing validation",
            **body,
        },
    )
    assert response.status_code == 400
    assert manager.registry.snapshot is old
    assert await redis.get(RedisConfigSource.KEY) is None


async def test_request_over_override_over_yaml(system):
    manager, _, _, app, client = system
    response = await client.put(
        f"/admin/deployments/{DEPLOYMENT}",
        json={
            "options": {"temperature": 0.8, "max_tokens": 64},
            "timeout": {"read": 90},
            "reason": "parameter experiment",
        },
    )
    assert response.status_code == 200
    deployment = manager.registry.resolve("qwen-7b")
    assert deployment.timeout.connect == 5
    assert deployment.timeout.read == 90
    assert deployment.options.top_p == 0.9
    await client.post("/v1/chat/completions", json={**BODY, "temperature": 0.3})
    adapter = app.state.adapters.get(deployment)
    assert adapter.calls[-1].temperature == 0.3
    assert adapter.calls[-1].max_tokens == 64
    await client.post("/v1/chat/completions", json=BODY)
    assert adapter.calls[-1].temperature == 0.8


async def test_put_replaces_only_target_and_deletes_require_reason(system):
    _, _, redis, _, client = system
    url = f"/admin/deployments/{DEPLOYMENT}"
    await client.put(url, json={"enabled": False, "weight": 10, "reason": "first"})
    response = await client.put(url, json={"weight": 20, "reason": "replace"})
    assert response.status_code == 200
    stored = json.loads(await redis.get(RedisConfigSource.KEY))
    assert stored["deployments"][DEPLOYMENT]["enabled"] is None
    assert stored["deployments"][DEPLOYMENT]["weight"] == 20
    assert (await client.post("/v1/chat/completions", json=BODY)).status_code == 200
    assert (await client.delete(url + "/override")).status_code == 400
    assert (
        await client.put(
            "/admin/deployments/unknown",
            json={
                "weight": 10,
                "reason": "unknown",
            },
        )
    ).status_code == 404


async def test_effective_and_sources_mask_nested_secrets(system):
    manager, path, _, _, client = system
    path.write_text(
        SAMPLE.replace("http://localhost:11434", "http://user:password@host:80?a=secret").replace(
            "keep_alive: 30m", "keep_alive: 30m\n          api_key: secret-value"
        ),
        encoding="utf-8",
    )
    await manager.reload()
    for url in ("/admin/config", "/admin/config/sources"):
        response = await client.get(url)
        assert response.status_code == 200
        assert "secret-value" not in response.text
        assert "password" not in response.text
        assert "a=secret" not in response.text
        assert "***" in response.text
    source = (await client.get("/admin/config/sources")).json()["base"]
    assert source["defaults"]["timeout"]["connect"] == 5
    assert "logical_model" not in source["models"]["qwen-7b"]["deployments"][0]
    assert redact({"max_tokens": 10}) == {"max_tokens": 10}
    assert redact({"headers": {"X-API-Key": "secret", "accessToken": "secret"}}) == {
        "headers": {"X-API-Key": "***", "accessToken": "***"},
    }


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/admin/config"),
        ("GET", "/admin/config/sources"),
        ("POST", "/admin/config/reload"),
        ("PUT", f"/admin/deployments/{DEPLOYMENT}"),
        ("DELETE", f"/admin/deployments/{DEPLOYMENT}/override?reason=test"),
        ("GET", "/admin/diagnosis"),
    ],
)
async def test_admin_always_requires_configured_valid_token(system, method, url):
    _, _, _, app, client = system
    client.headers.pop("Authorization")
    assert (await client.request(method, url)).status_code == 401
    client.headers["Authorization"] = "Bearer wrong"
    assert (await client.request(method, url)).status_code == 401
    app.state.settings.api_key = ""
    assert (await client.request(method, url)).status_code == 401
    assert (await client.get("/healthz")).status_code == 200


async def test_redis_unavailable_at_startup_uses_yaml(tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisConfigSource("redis://unused", client=redis)
    store.read = AsyncMock(side_effect=RedisConnectionError())
    source = LayeredConfigSource(YamlConfigSource(path), store)
    snapshot = await source.read()
    assert snapshot.models["qwen-7b"].deployments[0].enabled
    assert source.redis_available is False
    await store.close()


async def test_redis_outage_retains_override_until_original_expiry(system, monkeypatch):
    manager, _, _, _, client = system
    await client.put(
        f"/admin/deployments/{DEPLOYMENT}",
        json={
            "enabled": False,
            "reason": "temporary disable",
        },
    )
    monkeypatch.setattr(
        manager.source.override, "read", AsyncMock(side_effect=RedisConnectionError())
    )
    await manager.reload()
    assert manager.registry.snapshot.models["qwen-7b"].deployments[0].enabled is False
    manager.source.override_document = manager.source.override_document.model_copy(
        update={
            "expires_at": {DEPLOYMENT: datetime.now(UTC) - timedelta(seconds=1)},
        }
    )
    await manager.reload()
    assert manager.registry.resolve("qwen-7b").enabled


async def test_invalid_redis_document_retains_snapshot(system):
    manager, _, redis, _, _ = system
    previous = manager.registry.snapshot
    await redis.set(RedisConfigSource.KEY, '{"deployments": {"x": {"enabled": false}}}')
    with pytest.raises(ConfigError):
        await manager.reload()
    assert manager.registry.snapshot is previous


async def test_ttl_expiry_returns_to_yaml_without_pubsub(system):
    manager, _, redis, _, client = system
    await client.put(
        f"/admin/deployments/{DEPLOYMENT}",
        json={
            "enabled": False,
            "reason": "expire",
        },
    )
    await redis.delete(RedisConfigSource.KEY)
    applied = observe_reloads(manager, lambda: manager.registry.resolve("qwen-7b").enabled)
    # Poll only: no subscription can hide a missed notification.
    task = asyncio.create_task(manager.poll(0, 0.01))
    try:
        await asyncio.wait_for(applied.wait(), timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert manager.registry.resolve("qwen-7b").enabled


async def test_pubsub_updates_another_manager(system):
    manager, path, redis, _, _ = system
    second_source = LayeredConfigSource(YamlConfigSource(path), manager.source.override)
    second = ConfigManager(
        second_source, ModelRegistry(await second_source.read()), AdapterFactory()
    )
    ready = asyncio.Event()
    updated = asyncio.Event()
    original_reload = second.reload

    async def reload(trigger="admin", **kwargs):
        result = await original_reload(trigger, **kwargs)
        ready.set()
        if second.registry.resolve("qwen-7b").weight == 30:
            updated.set()
        return result

    second.reload = reload
    task = asyncio.create_task(second.subscribe())
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        await manager.change_override(
            DEPLOYMENT, DeploymentOverride(weight=30), actor="test", reason="cross-process"
        )
        await asyncio.wait_for(updated.wait(), timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_watch_file_recovers_after_invalid_yaml(system):
    manager, path, _, _, _ = system
    # Exercise the file-only path, rather than Redis reconciliation.
    store = manager.source.override
    manager.source.override = None
    failed = observe_reloads(manager, lambda: manager.last_error is not None)
    manager.start(0.01)
    try:
        old = manager.registry.snapshot
        path.write_text("broken: [", encoding="utf-8")
        await asyncio.wait_for(failed.wait(), timeout=2)
        assert manager.registry.snapshot is old
        recovered = observe_reloads(
            manager,
            lambda: manager.registry.resolve("qwen-7b").endpoint == "http://new-host:11434",
        )
        path.write_text(
            SAMPLE.replace("http://localhost:11434", "http://new-host:11434"), encoding="utf-8"
        )
        await asyncio.wait_for(recovered.wait(), timeout=2)
        assert manager.revision == 2
    finally:
        await manager.close()
        manager.source.override = store


async def test_100_inflight_requests_survive_endpoint_timeout_reload(system, monkeypatch):
    manager, path, _, app, _ = system
    release = asyncio.Event()
    all_started = asyncio.Event()
    old_closed = asyncio.Event()
    started = 0
    instances = []

    class BlockingAdapter(FakeAdapter):
        def __init__(self, deployment):
            super().__init__(deployment)
            self.closed = False
            instances.append(self)

        async def stream_chat(self, request):
            nonlocal started
            started += 1
            if started == 100:
                all_started.set()
            if self.deployment.endpoint == "http://localhost:11434":
                await release.wait()
            assert not self.closed
            async for chunk in super().stream_chat(request):
                yield chunk

        async def aclose(self):
            self.closed = True
            if self.deployment.endpoint == "http://localhost:11434":
                old_closed.set()

    monkeypatch.setitem(ADAPTER_REGISTRY, "ollama", BlockingAdapter)
    service = app.state.chat_service
    request = ChatCompletionRequest.model_validate(BODY)
    requests = [
        asyncio.create_task(service.complete(request, RequestContext(request_id=str(i))))
        for i in range(100)
    ]
    try:
        await asyncio.wait_for(all_started.wait(), timeout=2)
        old_adapter = instances[0]
        path.write_text(
            SAMPLE.replace("http://localhost:11434", "http://new:11434").replace(
                "read: 60", "read: 90"
            ),
            encoding="utf-8",
        )
        await manager.reload()
        assert old_adapter.closed is False
        new_response = await service.complete(request, RequestContext(request_id="new"))
        assert new_response.deployment.endpoint == "http://new:11434"
        assert instances[-1].deployment.timeout.read == 90
        release.set()
        responses = await asyncio.gather(*requests)
        assert len(responses) == 100
        assert all(r.deployment.endpoint == "http://localhost:11434" for r in responses)
        await asyncio.wait_for(old_closed.wait(), timeout=2)
    finally:
        release.set()
        await asyncio.gather(*requests, return_exceptions=True)


async def test_concurrent_redis_updates_keep_other_targets_and_ttls(system):
    manager, path, redis, _, _ = system
    path.write_text(
        SAMPLE
        + """
  second:
    deployments:
      - id: second
        adapter: ollama
        endpoint: http://localhost:11434
        upstream_model: other
""",
        encoding="utf-8",
    )
    await manager.reload()
    second_source = LayeredConfigSource(YamlConfigSource(path), manager.source.override)
    second = ConfigManager(
        second_source, ModelRegistry(await second_source.read()), AdapterFactory()
    )
    await asyncio.gather(
        manager.change_override(
            DEPLOYMENT, DeploymentOverride(weight=10), actor="a", reason="first", ttl_sec=100
        ),
        second.change_override(
            "second", DeploymentOverride(enabled=False), actor="b", reason="second", ttl_sec=200
        ),
    )
    stored = OverrideDocument.model_validate_json(await redis.get(RedisConfigSource.KEY))
    assert set(stored.deployments) == {DEPLOYMENT, "second"}
    assert stored.expires_at["second"] > stored.expires_at[DEPLOYMENT]
    assert 190 < await redis.ttl(RedisConfigSource.KEY) <= 201


async def test_noop_reload_preserves_revision_and_metrics_are_exposed(system):
    manager, _, _, _, client = system
    old = manager.registry.snapshot
    assert await manager.reload() is False
    assert manager.registry.snapshot is old
    assert manager.revision == 1
    metrics = (await client.get("/metrics")).text
    assert "llm_gateway_config_reload_total" in metrics
    assert "llm_gateway_config_version_timestamp" in metrics
    assert "llm_gateway_config_version 1.0" in metrics


async def test_lifespan_closes_config_tasks(tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    app = create_app(
        Settings(config_path=path, config_reload_sec=5, redis_url="", diagnosis_enabled=False)
    )
    async with app.router.lifespan_context(app):
        tasks = list(app.state.config_manager.tasks)
        assert len(tasks) == 1
    assert all(task.done() for task in tasks)


async def test_timeout_only_reload_recreates_adapter_and_stream_close_drains(system):
    manager, path, _, app, _ = system
    service = app.state.chat_service
    request = ChatCompletionRequest.model_validate({**BODY, "stream": True})
    stream = service.stream(request, RequestContext(request_id="stream"))
    await anext(stream)  # role chunk; lease must span this suspension
    old = app.state.adapters.get(manager.registry.resolve("qwen-7b"))
    closed = asyncio.Event()

    async def close():
        closed.set()

    old.aclose = close
    path.write_text(SAMPLE.replace("read: 60", "read: 90"), encoding="utf-8")
    await manager.reload()
    new = app.state.adapters.get(manager.registry.resolve("qwen-7b"))
    assert new is not old
    assert new.deployment.timeout.read == 90
    assert not closed.is_set()
    await stream.aclose()
    await asyncio.wait_for(closed.wait(), timeout=2)


async def test_expired_entry_does_not_wait_for_other_entry_ttl(system):
    manager, path, redis, _, _ = system
    path.write_text(
        SAMPLE
        + """
  second:
    deployments:
      - id: second
        adapter: ollama
        endpoint: http://localhost:11434
        upstream_model: other
""",
        encoding="utf-8",
    )
    await manager.reload()
    now = datetime.now(UTC)
    document = OverrideDocument(
        updated_at=now,
        updated_by="operator",
        reason="per-deployment TTL",
        deployments={
            DEPLOYMENT: DeploymentOverride(enabled=False),
            "second": DeploymentOverride(weight=30),
        },
        expires_at={DEPLOYMENT: now - timedelta(seconds=1), "second": now + timedelta(hours=1)},
    )
    await redis.set(RedisConfigSource.KEY, document.model_dump_json(), ex=3600)
    await manager.reload()
    assert manager.registry.resolve("qwen-7b").enabled
    assert manager.registry.resolve("second").weight == 30


async def test_failed_redis_write_retains_local_snapshot(system, monkeypatch):
    manager, _, _, _, client = system
    old = manager.registry.snapshot
    monkeypatch.setattr(
        manager.source.override, "update", AsyncMock(side_effect=RedisConnectionError())
    )
    response = await client.put(
        f"/admin/deployments/{DEPLOYMENT}",
        json={
            "enabled": False,
            "reason": "failed write",
        },
    )
    assert response.status_code == 500
    assert manager.registry.snapshot is old


@pytest.mark.parametrize("file_interval", [0, 60])
async def test_redis_poll_respects_independent_file_watch_interval(system, file_interval):
    manager, path, _, _, _ = system
    path.write_text(
        SAMPLE.replace("http://localhost:11434", "http://pending:11434"), encoding="utf-8"
    )
    polled = observe_reloads(manager, lambda: True)
    task = asyncio.create_task(manager.poll(file_interval, 0.01))
    try:
        await asyncio.wait_for(polled.wait(), timeout=2)
        assert manager.registry.resolve("qwen-7b").endpoint == "http://localhost:11434"
        assert (
            manager.sources()["base"]["models"]["qwen-7b"]["deployments"][0]["endpoint"]
            == "http://localhost:11434"
        )
        await manager.reload()
        assert manager.registry.resolve("qwen-7b").endpoint == "http://pending:11434"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_redis_pubsub_uses_accepted_yaml_during_invalid_file_edit(system):
    manager, path, redis, _, _ = system
    ready = observe_reloads(manager, lambda: True)
    task = asyncio.create_task(manager.subscribe())
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        path.write_text("models: [", encoding="utf-8")
        now = datetime.now(UTC)
        document = OverrideDocument(
            updated_at=now,
            updated_by="operator",
            reason="maintenance",
            deployments={DEPLOYMENT: DeploymentOverride(enabled=False)},
            expires_at={DEPLOYMENT: now + timedelta(hours=1)},
        )
        updated = observe_reloads(
            manager,
            lambda: not manager.registry.snapshot.models["qwen-7b"].deployments[0].enabled,
        )
        await redis.set(RedisConfigSource.KEY, document.model_dump_json())
        await redis.publish(RedisConfigSource.CHANNEL, "changed")
        await asyncio.wait_for(updated.wait(), timeout=2)
        assert not manager.registry.snapshot.models["qwen-7b"].deployments[0].enabled
        assert manager.source.base.is_stale()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("remove_file", [False, True])
async def test_invalid_or_missing_yaml_does_not_block_override_expiry(system, remove_file):
    manager, path, redis, _, client = system
    await client.put(
        f"/admin/deployments/{DEPLOYMENT}",
        json={"enabled": False, "reason": "temporary"},
    )
    if remove_file:
        path.unlink()
    else:
        path.write_text("models: [", encoding="utf-8")
    await redis.delete(RedisConfigSource.KEY)
    restored = observe_reloads(
        manager,
        lambda: manager.registry.snapshot.models["qwen-7b"].deployments[0].enabled,
    )
    failures = CONFIG_RELOAD.labels("poll", "failed")._value.get()
    task = asyncio.create_task(manager.poll(0.01, 0.01))
    try:
        await asyncio.wait_for(restored.wait(), timeout=2)
        assert manager.registry.resolve("qwen-7b").endpoint == "http://localhost:11434"
        assert CONFIG_RELOAD.labels("poll", "failed")._value.get() > failures
        assert manager.source.base.is_stale()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rejected_layer_merge_does_not_accept_file_mtime_or_raw(system):
    manager, path, redis, _, _ = system
    accepted = manager.sources()["base"]
    path.write_text(
        SAMPLE.replace("http://localhost:11434", "http://pending:11434"), encoding="utf-8"
    )
    await redis.set(RedisConfigSource.KEY, "invalid JSON")
    with pytest.raises(ConfigError):
        await manager.reload()
    assert manager.source.base.is_stale()
    assert manager.sources()["base"] == accepted
    await redis.delete(RedisConfigSource.KEY)
    await manager.reload("poll", refresh_base=False)
    assert manager.sources()["base"] == accepted
    assert manager.registry.resolve("qwen-7b").endpoint == "http://localhost:11434"
    await manager.reload()
    assert manager.registry.resolve("qwen-7b").endpoint == "http://pending:11434"
