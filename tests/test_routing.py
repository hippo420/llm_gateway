"""Phase 5 contracts: deterministic selection, API integration and live configuration."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections import Counter

import pytest

from llm_gateway.adapters.factory import AdapterFactory
from llm_gateway.core.context import RequestContext
from llm_gateway.core.errors import ConfigError, ModelNotFoundError, NoAvailableDeploymentError
from llm_gateway.observability.metrics import ROUTING_DECISIONS, ROUTING_NO_CANDIDATE
from llm_gateway.registry.loader import YamlConfigSource
from llm_gateway.registry.models import ModelEntry, ModelRegistry, RegistrySnapshot
from llm_gateway.routing.config import RoutingConfig
from llm_gateway.routing.decision import RoutingContext
from llm_gateway.routing.router import ModelRouter
from llm_gateway.routing.strategies.static import StaticStrategy
from llm_gateway.routing.strategies.weighted import WeightedStrategy
from llm_gateway.schemas.chat import ChatCompletionRequest
from llm_gateway.service.chat_service import ChatService

from .test_dynamic_config import BODY, DEPLOYMENT
from .test_dynamic_config import system as system
from .test_registry import SAMPLE

SECOND = "qwen-7b@second"
WEIGHTED_YAML = (
    SAMPLE.replace("weight: 100", "weight: 80")
    + f"""
      - id: {SECOND}
        adapter: ollama
        endpoint: http://localhost:11435
        upstream_model: qwen2.5:7b
        weight: 20
routing:
  strategy: weighted
  bucket_header: X-Session-Id
"""
)


@pytest.fixture
def candidates(deployment):
    return [
        deployment.model_copy(update={"id": "a", "weight": 80}),
        deployment.model_copy(update={"id": "b", "weight": 20}),
        deployment.model_copy(update={"id": "disabled", "enabled": False, "weight": 100}),
        deployment.model_copy(update={"id": "zero", "weight": 0}),
    ]


def test_static_uses_first_enabled_even_if_weight_zero(candidates):
    ordered = [candidates[2], candidates[3], candidates[1], candidates[0]]
    decision = StaticStrategy().select(RoutingContext("qwen-7b", "session"), ordered)
    assert decision.deployment.id == "zero"
    assert [d.id for d in decision.alternatives] == ["b", "a"]
    assert decision.reason == "first_enabled"


def test_weighted_distribution_and_alternatives(candidates):
    strategy = WeightedStrategy()
    counts = Counter()
    for index in range(10_000):
        decision = strategy.select(RoutingContext("qwen-7b", f"session-{index}"), candidates)
        counts[decision.deployment.id] += 1
        assert {decision.deployment.id, *(d.id for d in decision.alternatives)} == {"a", "b"}
    assert 7800 <= counts["a"] <= 8200
    assert 1800 <= counts["b"] <= 2200


def test_sticky_selection_survives_reordering_and_proportional_weights(candidates):
    strategy = WeightedStrategy()
    scaled = [d.model_copy(update={"weight": d.weight // 2}) for d in reversed(candidates)]
    for index in range(500):
        ctx = RoutingContext("qwen-7b", f"session-{index}")
        expected = strategy.select(ctx, candidates)
        assert strategy.select(ctx, scaled).deployment.id == expected.deployment.id
        assert (
            strategy.select(ctx, list(reversed(candidates))).alternatives == expected.alternatives
        )


def test_bucketing_is_independent_of_python_hash_seed(candidates):
    script = """
import json
from llm_gateway.registry.models import ModelDeployment
from llm_gateway.routing.decision import RoutingContext
from llm_gateway.routing.strategies.weighted import WeightedStrategy
import sys
candidates = [ModelDeployment.model_validate(d) for d in json.load(sys.stdin)]
print(json.dumps([
    WeightedStrategy().select(
        RoutingContext('qwen-7b', f'session-{i}'), candidates,
    ).deployment.id for i in range(100)
]))
"""
    outputs = []
    for seed in ("1", "123"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps([d.model_dump() for d in candidates]),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"},
        )
        outputs.append(json.loads(result.stdout))
    expected = [
        WeightedStrategy()
        .select(RoutingContext("qwen-7b", f"session-{i}"), candidates)
        .deployment.id
        for i in range(100)
    ]
    assert outputs == [expected, expected]


@pytest.mark.parametrize("strategy", [StaticStrategy(), WeightedStrategy()])
def test_empty_or_disabled_candidates_raise_gw4004(strategy, candidates):
    for options in ([], [candidates[2]]):
        with pytest.raises(NoAvailableDeploymentError) as exc:
            strategy.select(RoutingContext("qwen-7b", "session"), options)
        assert exc.value.code == "GW-4004"


def test_weighted_all_zero_fails_closed(candidates):
    with pytest.raises(NoAvailableDeploymentError):
        WeightedStrategy().select(RoutingContext("qwen-7b", "session"), [candidates[3]])


@pytest.mark.parametrize(
    "routing",
    [
        "strategy: typo",
        "strategy: health_aware",
        "strategy: metric_aware",
        "strategy: weighted\n  unknown: true",
        'bucket_header: ""',
        'bucket_header: "X Session"',
        'bucket_header: "X:Header"',
    ],
)
def test_invalid_routing_config_is_rejected_at_load(tmp_path, routing):
    path = tmp_path / "gateway.yaml"
    path.write_text(SAMPLE + "\nrouting:\n  " + routing + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        YamlConfigSource(path).load()


def test_non_100_sum_is_normalized_with_configuration_warning(tmp_path, caplog):
    path = tmp_path / "gateway.yaml"
    path.write_text(WEIGHTED_YAML.replace("weight: 80", "weight: 40"), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        snapshot = YamlConfigSource(path).load()
    warnings = [r for r in caplog.records if getattr(r, "event", "") == "config_weights_normalized"]
    assert len(warnings) == 1
    assert warnings[0].total_weight == 60
    assert snapshot.routing.strategy == "weighted"


def test_router_uses_supplied_snapshot_and_bounds_failure_metrics(registry):
    router = ModelRouter(registry)
    previous = registry.snapshot
    registry.swap(previous.model_copy(update={"models": {}}))
    decision = router.route(RoutingContext("qwen-7b", "session"), snapshot=previous)
    assert decision.deployment.id == "qwen-7b@fake"
    before = ROUTING_NO_CANDIDATE.labels("qwen-off")._value.get()
    with pytest.raises(NoAvailableDeploymentError):
        router.route(RoutingContext("qwen-off", "session"), snapshot=previous)
    assert ROUTING_NO_CANDIDATE.labels("qwen-off")._value.get() == before + 1
    with pytest.raises(ModelNotFoundError):
        router.route(RoutingContext("untrusted-random-model", "session"))
    samples = [sample for family in ROUTING_NO_CANDIDATE.collect() for sample in family.samples]
    assert all(s.labels.get("model") != "untrusted-random-model" for s in samples)


async def configure_weighted(system):
    manager, path, _, _, _ = system
    path.write_text(WEIGHTED_YAML, encoding="utf-8")
    await manager.reload()


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_sticky_selects_once_with_log_and_metrics(system, stream, caplog):
    await configure_weighted(system)
    manager, _, _, _, client = system
    expected = (
        WeightedStrategy()
        .select(
            RoutingContext("qwen-7b", "private-session"),
            manager.registry.candidates("qwen-7b"),
        )
        .deployment.id
    )
    counter = ROUTING_DECISIONS.labels("qwen-7b", expected, "weighted")
    before = counter._value.get()
    caplog.set_level(logging.INFO)
    for index in range(3):
        response = await client.post(
            "/v1/chat/completions",
            json={**BODY, "stream": stream},
            headers={"X-Session-Id": "private-session", "X-Request-Id": f"request-{index}"},
        )
        assert response.status_code == 200
        assert response.headers["X-Gateway-Deployment"] == expected
        if stream:
            assert response.text.endswith("data: [DONE]\n\n")
        else:
            assert response.json()["model"] == "qwen-7b"
    assert counter._value.get() == before + 3
    records = [r for r in caplog.records if getattr(r, "event", "") == "routing_decision"]
    assert len(records) == 3
    assert all(r.reason == "sticky_weighted_bucket" and len(r.alternatives) == 1 for r in records)
    assert "private-session" not in repr([r.__dict__ for r in records])
    exposed = (await client.get("/metrics")).text
    assert "llm_gateway_routing_decision_total" in exposed
    assert "private-session" not in exposed


@pytest.mark.parametrize(
    ("headers", "bucket"),
    [
        ({"X-Session-Id": "session", "X-User-Bucket": "user"}, "session"),
        ({"X-User-Bucket": "user"}, "user"),
        ({"X-Session-Id": "bad\tvalue", "X-User-Bucket": "user"}, "user"),
        ({"X-Session-Id": "x" * 129, "X-User-Bucket": "bad value"}, "request"),
        ({}, "request"),
    ],
)
async def test_header_precedence_and_invalid_hints(system, headers, bucket, monkeypatch):
    await configure_weighted(system)
    manager, _, _, app, client = system
    router = app.state.chat_service._router
    original_route = router.route
    contexts = []

    def capture(ctx, **kwargs):
        contexts.append(ctx)
        return original_route(ctx, **kwargs)

    monkeypatch.setattr(router, "route", capture)
    expected = (
        WeightedStrategy()
        .select(
            RoutingContext("qwen-7b", bucket),
            manager.registry.candidates("qwen-7b"),
        )
        .deployment.id
    )
    response = await client.post(
        "/v1/chat/stream",
        json=BODY,
        headers={"X-Request-Id": "request", **headers},
    )
    assert response.status_code == 200
    assert response.headers["X-Gateway-Deployment"] == expected
    assert len(contexts) == 1
    assert contexts[0].bucket_key == bucket


async def test_custom_bucket_header_and_strategy_reload(system):
    await configure_weighted(system)
    manager, path, _, _, client = system
    path.write_text(WEIGHTED_YAML.replace("X-Session-Id", "X-Tenant-Key"), encoding="utf-8")
    await manager.reload()
    for index in range(30):
        key = f"tenant-{index}"
        expected = (
            WeightedStrategy()
            .select(
                RoutingContext("qwen-7b", key),
                manager.registry.candidates("qwen-7b"),
            )
            .deployment.id
        )
        response = await client.post(
            "/v1/chat",
            json=BODY,
            headers={"x-tenant-key": key, "X-Session-Id": "ignored", "X-User-Bucket": "ignored"},
        )
        assert response.headers["X-Gateway-Deployment"] == expected
    path.write_text(
        WEIGHTED_YAML.replace("strategy: weighted", "strategy: static"), encoding="utf-8"
    )
    await manager.reload()
    for index in range(5):
        response = await client.post("/v1/chat", json=BODY, headers={"X-Session-Id": f"s-{index}"})
        assert response.headers["X-Gateway-Deployment"] == DEPLOYMENT


async def test_redis_weights_disable_delete_and_expiry_change_routing(system):
    await configure_weighted(system)
    manager, _, redis, _, client = system
    selected = next(
        f"session-{i}"
        for i in range(100)
        if WeightedStrategy()
        .select(
            RoutingContext("qwen-7b", f"session-{i}"),
            manager.registry.candidates("qwen-7b"),
        )
        .deployment.id
        == DEPLOYMENT
    )
    headers = {"X-Session-Id": selected}
    url = f"/admin/deployments/{DEPLOYMENT}"
    for patch in ({"weight": 0}, {"enabled": False}):
        response = await client.put(url, json={**patch, "reason": "routing experiment"})
        assert response.status_code == 200
        response = await client.post("/v1/chat", json=BODY, headers=headers)
        assert response.headers["X-Gateway-Deployment"] == SECOND
        response = await client.delete(url + "/override", params={"reason": "restore"})
        assert response.status_code == 200
        response = await client.post("/v1/chat", json=BODY, headers=headers)
        assert response.headers["X-Gateway-Deployment"] == DEPLOYMENT
    await client.put(url, json={"weight": 0, "reason": "expires"})
    await redis.delete(manager.source.override.KEY)
    await manager.reload("poll", refresh_base=False)
    response = await client.post("/v1/chat", json=BODY, headers=headers)
    assert response.headers["X-Gateway-Deployment"] == DEPLOYMENT


@pytest.mark.parametrize("patch", [{"weight": 0}, {"enabled": False}])
async def test_no_candidates_fails_before_sse_headers(system, patch):
    await configure_weighted(system)
    _, _, _, _, client = system
    for deployment_id in (DEPLOYMENT, SECOND):
        response = await client.put(
            f"/admin/deployments/{deployment_id}",
            json={**patch, "reason": "no candidates"},
        )
        assert response.status_code == 200
    response = await client.post("/v1/chat/completions", json={**BODY, "stream": True})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GW-4004"
    assert "X-Gateway-Deployment" not in response.headers


async def test_invalid_strategy_reload_retains_router_behavior(system):
    await configure_weighted(system)
    manager, path, _, _, client = system
    previous = manager.registry.snapshot
    path.write_text(WEIGHTED_YAML.replace("strategy: weighted", "strategy: typo"), encoding="utf-8")
    response = await client.post("/admin/config/reload")
    assert response.status_code == 500
    assert manager.registry.snapshot is previous
    assert (await client.post("/v1/chat", json=BODY)).status_code == 200


def test_context_keeps_decision_and_alternatives_from_selected_generation(candidates):
    registry = ModelRegistry(
        RegistrySnapshot(
            version=1,
            loaded_at="test",
            routing=RoutingConfig(strategy="weighted"),
            models={"qwen-7b": ModelEntry(name="qwen-7b", deployments=candidates)},
        )
    )
    service = ChatService(registry, AdapterFactory())
    ctx = RequestContext(request_id="request", session_id="session", request_type="simple_qa")
    selected = service.prepare(ChatCompletionRequest.model_validate(BODY), ctx)
    assert ctx.routing_decision.deployment is selected
    assert len(ctx.routing_decision.alternatives) == 1
    previous_decision = ctx.routing_decision
    registry.swap(registry.snapshot.model_copy(update={"models": {}}))
    assert ctx.routing_decision is previous_decision
    assert {selected.id, ctx.routing_decision.alternatives[0].id} == {"a", "b"}


async def test_http_distribution_matches_configured_weights(system):
    await configure_weighted(system)
    _, _, _, _, client = system
    counts = Counter()
    for index in range(1000):
        response = await client.post(
            "/v1/chat",
            json=BODY,
            headers={"X-Session-Id": f"session-{index}"},
        )
        assert response.status_code == 200
        counts[response.headers["X-Gateway-Deployment"]] += 1
    assert 760 <= counts[DEPLOYMENT] <= 840
    assert 160 <= counts[SECOND] <= 240


async def test_prepared_stream_keeps_selection_when_weights_change(system):
    await configure_weighted(system)
    manager, _, _, app, client = system
    service = app.state.chat_service
    request = ChatCompletionRequest.model_validate({**BODY, "stream": True})
    ctx = RequestContext(request_id="prepared", session_id="session")
    deployment = service.prepare(request, ctx)
    decision = ctx.routing_decision
    response = await client.put(
        f"/admin/deployments/{deployment.id}",
        json={"enabled": False, "reason": "after prepare"},
    )
    assert response.status_code == 200
    chunks = [chunk async for chunk in service.stream(request, ctx, deployment)]
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert ctx.routing_decision is decision
    assert ctx.deployment_id == deployment.id
    next_ctx = RequestContext(request_id="next", session_id="session")
    assert service.prepare(request, next_ctx).id != deployment.id


def test_request_hints_are_available_without_becoming_routing_rules(registry, monkeypatch):
    service = ChatService(registry, AdapterFactory())
    original_route = service._router.route
    captured = []

    def capture(ctx, **kwargs):
        captured.append(ctx)
        return original_route(ctx, **kwargs)

    monkeypatch.setattr(service._router, "route", capture)
    service.prepare(
        ChatCompletionRequest.model_validate({**BODY, "max_tokens": 128}),
        RequestContext(request_id="request", request_type="report_analysis"),
    )
    assert captured[0].request_type == "report_analysis"
    assert captured[0].max_tokens == 128
    assert captured[0].estimated_input_tokens == 2  # "hello": ceil(5 / 4), only a heuristic.
    assert captured[0].requested_at.tzinfo is not None
