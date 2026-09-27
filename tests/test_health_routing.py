"""Health-aware admission with bounded evidence and shared retry/fallback observations."""

from __future__ import annotations

import asyncio

import pytest
import yaml

from llm_gateway.adapters.base import AdapterChatChunk
from llm_gateway.core.context import RequestContext
from llm_gateway.core.errors import ConfigError, NoAvailableDeploymentError, UpstreamError
from llm_gateway.registry.loader import YamlConfigSource
from llm_gateway.registry.models import ModelEntry, ModelRegistry, RegistrySnapshot
from llm_gateway.routing.config import RoutingConfig
from llm_gateway.routing.decision import RoutingContext
from llm_gateway.routing.health import HealthTracker
from llm_gateway.routing.router import ModelRouter
from llm_gateway.routing.strategies.weighted import WeightedStrategy
from llm_gateway.schemas.chat import ChatCompletionRequest

from .test_dynamic_config import BODY, DEPLOYMENT
from .test_dynamic_config import system as system
from .test_resilience import CONFIG, SECOND, THIRD
from .test_resilience import fault_system as fault_system
from .test_routing import WEIGHTED_YAML


def health_config(ids, **changes):
    return RoutingConfig.model_validate(
        {
            "strategy": "health_aware",
            "health_aware": {
                "window_sec": 10,
                "min_samples": 2,
                "max_samples": 20,
                "thresholds": {
                    name: {"max_error_rate": 0.5, "max_latency_p95_sec": 2} for name in ids
                },
                **changes,
            },
        }
    )


@pytest.fixture
def tracked(deployment):
    deps = [deployment.model_copy(update={"id": name, "weight": 50}) for name in ("a", "b")]
    registry = ModelRegistry(
        RegistrySnapshot(
            version=1,
            loaded_at="test",
            routing=health_config([d.id for d in deps]),
            models={"qwen-7b": ModelEntry(name="qwen-7b", deployments=deps)},
        )
    )
    now = [100.0]
    health = HealthTracker(registry, clock=lambda: now[0])
    health.sync(registry.snapshot)
    return registry, health, deps, now


def test_minimum_evidence_boundaries_expiry_and_reentry(tracked):
    _, health, (a, _), now = tracked
    assert health.assess(a).error_rate is None
    health.observe(a, False, 1)
    assert health.assess(a).error_rate is None
    assert health.available(a)
    health.observe(a, True, 1)
    assert health.assess(a).error_rate == 0.5
    assert health.assess(a).latency_p95_sec is None  # independent successful sample minimum
    assert health.available(a)  # only strictly exceeding a limit excludes
    health.observe(a, False, 1)
    assert not health.available(a)
    assert health.assess(a).reasons == ("error_rate",)
    now[0] += 10
    assert health.available(a)
    assert health.assess(a).samples == 0
    assert health.assess(a).error_rate is None


def test_successful_latency_p95_ignores_failed_durations_and_caps_samples(tracked):
    _, health, (a, _), _ = tracked
    for _ in range(18):
        health.observe(a, True, 1)
    health.observe(a, True, 2)
    health.observe(a, True, 100)
    assert health.assess(a).latency_p95_sec == 2  # nearest rank of 20 successes
    assert health.available(a)
    health.observe(a, False, 9999)
    assert health.assess(a).samples == 20
    assert health.assess(a).successful_samples == 19
    assert health.assess(a).latency_p95_sec == 100
    assert health.assess(a).reasons == ("latency_p95",)


def test_health_filter_keeps_weighted_stickiness_and_removes_all_bad_candidates(tracked):
    registry, health, (a, b), _ = tracked
    router = ModelRouter(registry, health=health)
    for index in range(100):
        ctx = RoutingContext("qwen-7b", f"session-{index}")
        assert router.route(ctx).deployment == WeightedStrategy().select(ctx, [a, b]).deployment
    for _ in range(2):
        health.observe(a, False, 1)
    decision = router.route(RoutingContext("qwen-7b", "sticky"))
    assert decision.strategy == "health_aware"
    assert decision.deployment == b and decision.alternatives == ()
    for _ in range(2):
        health.observe(b, True, 3)
    with pytest.raises(NoAvailableDeploymentError):
        router.route(RoutingContext("qwen-7b", "sticky"))


def test_health_history_survives_weights_but_not_endpoint_or_threshold_changes(tracked):
    registry, health, (a, b), _ = tracked
    for _ in range(2):
        health.observe(a, False, 1)

    def swap(deployment):
        registry.swap(
            registry.snapshot.model_copy(
                update={
                    "models": {"qwen-7b": ModelEntry(name="qwen-7b", deployments=[deployment, b])}
                }
            )
        )

    a2 = a.model_copy(update={"weight": 10, "enabled": False})
    swap(a2)
    assert not health.available(a2)
    a3 = a.model_copy(update={"endpoint": "http://replacement:11434"})
    swap(a3)
    assert health.available(a3)
    health.observe(a, False, 1)  # old in-flight endpoint cannot contaminate replacement
    assert health.assess(a3).samples == 0
    health.observe(a3, False, 1)
    registry.swap(
        registry.snapshot.model_copy(update={"routing": health_config(["a", "b"], window_sec=30)})
    )
    assert health.available(a3)
    assert health.assess(a3).samples == 0
    swap(b)
    health.sync(registry.snapshot)
    assert "a" not in health.report()["deployments"]


@pytest.mark.parametrize(
    "change",
    [
        {"min_samples": 0},
        {"min_samples": True},
        {"min_samples": 21, "max_samples": 20},
        {"max_samples": 10001},
        {"window_sec": float("nan")},
        {"thresholds": {}},
        {"thresholds": {DEPLOYMENT: {}}},
        {"thresholds": {DEPLOYMENT: {"max_error_rate": 1.1}}},
        {"thresholds": {DEPLOYMENT: {"max_latency_p95_sec": 0}}},
        {"thresholds": {DEPLOYMENT: {"max_latency_p95_sec": float("inf")}}},
        {"thresholds": {"unknown": {"max_error_rate": 0.5}}},
        {"thresholds": {DEPLOYMENT: {"max_error_rate": 0.5}}},  # missing second deployment
    ],
)
def test_invalid_health_config_rejected_at_load(tmp_path, change):
    raw = yaml.safe_load(WEIGHTED_YAML)
    ids = [d["id"] for d in raw["models"]["qwen-7b"]["deployments"]]
    raw["routing"] = health_config(ids).model_dump()
    raw["routing"]["health_aware"].update(change)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError):
        YamlConfigSource(path).load()


async def configure_fault_health(system, *, strategy="health_aware", minimum=2):
    manager, path, _, _, _ = system
    raw = yaml.safe_load(CONFIG)
    raw["routing"] = health_config([DEPLOYMENT, SECOND, THIRD], min_samples=minimum).model_dump()
    raw["routing"]["strategy"] = strategy
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    await manager.reload()


def primary_session(manager):
    return next(
        f"session-{i}"
        for i in range(100)
        if WeightedStrategy()
        .select(RoutingContext("qwen-7b", f"session-{i}"), manager.registry.candidates("qwen-7b"))
        .deployment.id
        == DEPLOYMENT
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_failures_recorded_per_attempt_and_health_prevents_retry(
    fault_system, system, stream
):
    manager, _, client, plans, attempts, _, _ = fault_system
    await configure_fault_health(system, minimum=1)
    plans[DEPLOYMENT] = [UpstreamError("unavailable", detail={"upstream_status": 503})]
    headers = {"X-Session-Id": primary_session(manager)}
    response = await client.post("/v1/chat", json={**BODY, "stream": stream}, headers=headers)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Fallback"] == SECOND
    assert attempts == {DEPLOYMENT: 1, SECOND: 1}
    status = (await client.get("/admin/routing/status")).json()
    assert status["scope"] == "process"
    assert status["deployments"][DEPLOYMENT]["error_rate"] == 1
    assert status["deployments"][DEPLOYMENT]["health_filter_admitted"] is False
    assert status["deployments"][SECOND]["error_rate"] == 0
    again = await client.post("/v1/chat", json=BODY, headers=headers)
    assert again.status_code == 200
    assert again.headers["X-Gateway-Deployment"] != DEPLOYMENT


async def test_fallback_rechecks_health_after_initial_selection(fault_system, system):
    manager, app, client, plans, attempts, _, _ = fault_system
    await configure_fault_health(system, minimum=1)
    second = next(d for d in manager.registry.candidates("qwen-7b") if d.id == SECOND)

    async def fail_and_mark_alternative():
        app.state.chat_service._health.observe(second, False, 1)
        raise UpstreamError("unavailable", detail={"upstream_status": 503})
        yield  # async generator

    plans[DEPLOYMENT] = [fail_and_mark_alternative]
    response = await client.post(
        "/v1/chat", json=BODY, headers={"X-Session-Id": primary_session(manager)}
    )
    assert response.status_code == 200
    assert response.headers["X-Gateway-Fallback"] == THIRD
    assert attempts == {DEPLOYMENT: 1, THIRD: 1}


async def test_shadow_observations_do_not_change_static_routing(fault_system, system):
    _, _, client, plans, attempts, _, _ = fault_system
    await configure_fault_health(system, strategy="static", minimum=1)
    plans[DEPLOYMENT] = [UpstreamError("unavailable", detail={"upstream_status": 503})]
    assert (await client.post("/v1/chat", json=BODY)).status_code == 200
    assert attempts == {DEPLOYMENT: 2}
    status = (await client.get("/admin/routing/status")).json()
    assert status["health_filter_active"] is False
    assert status["deployments"][DEPLOYMENT]["samples"] == 2


@pytest.mark.parametrize("action", ["client_error", "cancel", "partial_error"])
async def test_neutral_and_partial_attempts(fault_system, system, action):
    manager, app, client, plans, _, _, _ = fault_system
    await configure_fault_health(system, minimum=1)

    async def cancel():
        raise asyncio.CancelledError
        yield

    plans[DEPLOYMENT] = [
        {
            "client_error": UpstreamError("bad input", detail={"upstream_status": 400}),
            "cancel": cancel,
            "partial_error": [AdapterChatChunk(delta="partial"), UpstreamError("upstream failed")],
        }[action]
    ]
    await client.post(
        "/v1/chat",
        json={**BODY, "stream": True},
        headers={"X-Session-Id": primary_session(manager)},
    )
    status = app.state.chat_service.routing_status()["deployments"][DEPLOYMENT]
    assert status["samples"] == (1 if action == "partial_error" else 0)


async def test_all_excluded_is_json_error_before_sse_and_stale_samples_recover(
    fault_system, system
):
    manager, app, client, _, _, _, _ = fault_system
    await configure_fault_health(system, minimum=1)
    health = app.state.chat_service._health
    now = [100.0]
    health.clock = lambda: now[0]
    for dep in manager.registry.candidates("qwen-7b"):
        health.observe(dep, False, 1)
    response = await client.post("/v1/chat", json={**BODY, "stream": True})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GW-4004"
    now[0] += 10
    response = await client.post("/v1/chat", json=BODY)
    assert response.status_code == 200


async def test_status_requires_admin_auth_and_exposes_no_endpoint(fault_system, system):
    _, _, client, _, _, _, _ = fault_system
    await configure_fault_health(system)
    for authorization in ("", "Bearer wrong"):
        response = await client.get(
            "/admin/routing/status", headers={"Authorization": authorization}
        )
        assert response.status_code == 401
    response = await client.get("/admin/routing/status")
    assert response.status_code == 200
    assert "http://" not in response.text
    assert "admin-secret" not in response.text


async def test_consumer_delay_is_excluded_from_health_latency(fault_system, system):
    manager, app, _, _, _, _, _ = fault_system
    await configure_fault_health(system, minimum=1)
    service = app.state.chat_service
    ctx = RequestContext(request_id=primary_session(manager))
    request = ChatCompletionRequest.model_validate(BODY)
    async for _ in service.stream(request, ctx):
        await asyncio.sleep(0.02)
    status = service.routing_status()["deployments"][ctx.deployment_id]
    assert status["successful_samples"] == 1
    assert status["latency_p95_sec"] < 0.03


async def test_strategy_reload_preserves_observations_and_invalid_reload_rolls_back(
    fault_system, system
):
    manager, app, client, _, _, _, _ = fault_system
    await configure_fault_health(system, strategy="weighted", minimum=1)
    health = app.state.chat_service._health
    for dep in manager.registry.candidates("qwen-7b"):
        health.observe(dep, False, 1)
    assert health.report()["health_filter_active"] is False
    await configure_fault_health(system, minimum=1)
    assert (await client.post("/v1/chat", json=BODY)).status_code == 503
    status = health.report()
    assert status["health_filter_active"] is True
    assert all(d["samples"] == 1 for d in status["deployments"].values())
    previous = manager.registry.snapshot
    path = system[1]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["routing"]["health_aware"]["thresholds"].pop(SECOND)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    response = await client.post("/admin/config/reload")
    assert response.status_code == 500
    assert manager.registry.snapshot is previous
    assert health.report() == status
