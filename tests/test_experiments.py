from __future__ import annotations

from collections import Counter

import pytest
import yaml
from prometheus_client import REGISTRY

from llm_gateway.core.context import RequestContext
from llm_gateway.core.errors import ConfigError, UpstreamUnavailableError
from llm_gateway.core.timing import ChatTimings
from llm_gateway.experiment.assignment import choose
from llm_gateway.experiment.manager import ExperimentManager
from llm_gateway.experiment.models import Experiment
from llm_gateway.registry.loader import YamlConfigSource

from .test_dynamic_config import BODY, DEPLOYMENT
from .test_dynamic_config import system as system
from .test_resilience import CONFIG, SECOND
from .test_resilience import fault_system as fault_system

NAME = "serving-compare-001"
EXPERIMENT = {
    "enabled": True,
    "bucket_key": "session_id",
    "variants": [
        {"name": "control", "deployment_id": DEPLOYMENT, "weight": 50},
        {"name": "treatment", "deployment_id": SECOND, "weight": 50},
    ],
    "guardrail": {"error_rate_max": 0.5, "min_requests": 2},
}


async def configure(system, **changes):
    manager, path, _, _, _ = system
    raw = yaml.safe_load(CONFIG)
    raw["experiments"] = {NAME: {**EXPERIMENT, **changes}}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    await manager.reload()
    return manager.registry.snapshot.experiments[NAME]


def key_for(experiment, variant):
    return next(f"s-{i}" for i in range(100) if choose(NAME, experiment, f"s-{i}").name == variant)


def test_hash_distribution_reordering_and_experiment_salt():
    config = Experiment.model_validate(EXPERIMENT)
    reordered = config.model_copy(update={"variants": list(reversed(config.variants))})
    counts = Counter()
    differences = 0
    for index in range(10000):
        bucket = f"session-{index}"
        result = choose(NAME, config, bucket)
        assert choose(NAME, reordered, bucket) == result
        counts[result.name] += 1
        differences += choose("another-experiment", config, bucket) != result
    assert 4700 <= counts["treatment"] <= 5300
    assert differences > 4000


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("variant", ["control", "treatment"])
async def test_assignment_headers_metrics_report_and_stickiness(system, stream, variant):
    config = await configure(system)
    _, _, _, _, client = system
    key = key_for(config, variant)
    target = DEPLOYMENT if variant == "control" else SECOND
    labels = {
        "model": "qwen-7b",
        "deployment_id": target,
        "adapter": "ollama",
        "stream": str(stream).lower(),
        "status": "success",
        "experiment": NAME,
        "variant": variant,
    }
    before = REGISTRY.get_sample_value("llm_gateway_requests_total", labels) or 0
    for _ in range(3):
        response = await client.post(
            "/v1/chat", json={**BODY, "stream": stream}, headers={"X-Session-Id": key}
        )
        assert response.status_code == 200
        assert response.headers["X-Gateway-Experiment"] == NAME
        assert response.headers["X-Gateway-Variant"] == variant
        assert response.headers["X-Gateway-Deployment"] == target
    assert REGISTRY.get_sample_value("llm_gateway_requests_total", labels) == before + 3
    report = (await client.get("/admin/experiments")).json()["experiments"][NAME]
    assert report["variants"][variant]["samples"] == 3
    assert report["variants"][variant]["input_tokens"] == 36
    assert key not in (await client.get("/metrics")).text


async def test_fallback_does_not_hide_treatment_failure_and_guardrail_latches(fault_system, system):
    _, app, client, plans, attempts, _, _ = fault_system
    config = await configure(system)
    key = key_for(config, "treatment")
    plans[SECOND] = [UpstreamUnavailableError("offline") for _ in range(4)]
    for _ in range(2):
        response = await client.post("/v1/chat", json=BODY, headers={"X-Session-Id": key})
        assert response.status_code == 200
        assert response.headers["X-Gateway-Variant"] == "treatment"
        assert response.headers["X-Gateway-Fallback"] == DEPLOYMENT
    manager = app.state.chat_service._experiments
    report = manager.report()["experiments"][NAME]
    assert report["paused"] and report["reason"] == "error_rate"
    assert report["variants"]["treatment"]["error_rate"] == 1
    assert report["variants"]["treatment"]["ttft_p95_sec"] is None
    response = await client.post("/v1/chat", json=BODY, headers={"X-Session-Id": key})
    assert response.headers["X-Gateway-Variant"] == "control"
    assert attempts[SECOND] == 4
    manager.clock = lambda: 10**12
    assert manager.report()["experiments"][NAME]["paused"]
    assert manager.report()["experiments"][NAME]["variants"]["treatment"]["samples"] == 0
    await client.put(f"/admin/deployments/{DEPLOYMENT}", json={"enabled": False, "reason": "test"})
    assert (
        await client.post("/v1/chat", json=BODY)
    ).status_code == 503  # no paused treatment rescue


async def test_ttft_warmup_minimum_cancellation_and_old_generation(system):
    await configure(
        system,
        warmup_requests=1,
        guardrail={"ttft_p95_max_sec": 1, "min_requests": 2, "max_samples": 3},
    )
    registry = system[0].registry
    manager = ExperimentManager(registry, clock=lambda: 100)
    config = registry.snapshot.experiments[NAME]
    key = key_for(config, "treatment")

    def ctx():
        value = RequestContext(request_id="r", session_id=key)
        value.assignment = manager.assign("qwen-7b", value, registry.snapshot)
        value.deployment_id = SECOND
        return value

    for status in ("success", "cancelled", "success"):
        manager.record(ctx(), status, ChatTimings(ttft_sec=2, total_sec=3))
    assert not manager.report()["experiments"][NAME]["paused"]
    late = ctx()
    current = ctx()
    manager.record(current, "success", ChatTimings(ttft_sec=2, total_sec=3))
    manager.record(current, "success", ChatTimings(ttft_sec=2, total_sec=3))  # recorded only once
    report = manager.report()["experiments"][NAME]
    assert report["paused"] and report["reason"] == "ttft_p95"
    assert report["variants"]["treatment"]["samples"] == 2
    await configure(system, guardrail={"ttft_p95_max_sec": 0.1, "min_requests": 1})
    manager.record(late, "success", ChatTimings(ttft_sec=3))
    assert not manager.report()["experiments"][NAME]["paused"]


@pytest.mark.parametrize(
    "bucket_key,headers",
    [
        ("session_id", {"X-Session-Id": "KEY"}),
        ("user_id", {"X-User-Bucket": "KEY"}),
        ("request_id", {"X-Request-Id": "KEY"}),
        ("session_id", {"X-Session-Id": "bad value", "X-Request-Id": "KEY"}),
    ],
)
async def test_bucket_key_mapping(system, bucket_key, headers):
    config = await configure(system, bucket_key=bucket_key)
    response = await system[4].post("/v1/chat", json=BODY, headers=headers)
    assert response.headers["X-Gateway-Variant"] == choose(NAME, config, "KEY").name


@pytest.mark.parametrize(
    "change",
    [
        {"bucket_key": "invalid"},
        {"control": "missing"},
        {"warmup_requests": -1},
        {"guardrail": {}},
        {"guardrail": {"error_rate_max": float("nan")}},
        {"guardrail": {"error_rate_max": 2}},
        {"guardrail": {"error_rate_max": 0.1, "min_requests": 5, "max_samples": 2}},
        {
            "variants": [
                {"name": "control", "deployment_id": "unknown", "weight": 50},
                {"name": "treatment", "deployment_id": SECOND, "weight": 50},
            ]
        },
        {
            "variants": [
                {"name": "control", "deployment_id": DEPLOYMENT, "weight": 0},
                {"name": "treatment", "deployment_id": SECOND, "weight": 0},
            ]
        },
    ],
)
def test_invalid_experiment_config(tmp_path, change):
    raw = yaml.safe_load(CONFIG)
    raw["experiments"] = {NAME: {**EXPERIMENT, **change}}
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError):
        YamlConfigSource(path).load()


async def test_duplicate_model_experiment_reload_rejected_and_admin_auth(system):
    await configure(system)
    manager, path, _, _, client = system
    previous = manager.registry.snapshot
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["experiments"]["duplicate"] = EXPERIMENT
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert (await client.post("/admin/config/reload")).status_code == 500
    assert manager.registry.snapshot is previous
    assert (
        await client.get("/admin/experiments", headers={"Authorization": ""})
    ).status_code == 401
    assert (await client.get("/admin/experiments")).status_code == 200


async def test_no_candidates_keeps_experiment_error_headers_and_counts_error(system):
    config = await configure(system)
    client = system[4]
    for dep in (DEPLOYMENT, SECOND):
        await client.put(f"/admin/deployments/{dep}", json={"enabled": False, "reason": "test"})
    response = await client.post(
        "/v1/chat",
        json={**BODY, "stream": True},
        headers={"X-Session-Id": key_for(config, "treatment")},
    )
    assert response.status_code == 503
    assert response.headers["X-Gateway-Variant"] == "treatment"
    report = (await client.get("/admin/experiments")).json()["experiments"][NAME]
    assert report["variants"]["treatment"]["outcomes"]["error"] == 1
    assert report["variants"]["treatment"]["outcomes"]["fallback"] == 0


async def test_disabled_assignment_routes_to_control_without_rebucketing(system):
    config = await configure(system)
    client = system[4]
    await client.put(f"/admin/deployments/{SECOND}", json={"weight": 0, "reason": "test"})
    response = await client.post(
        "/v1/chat", json=BODY, headers={"X-Session-Id": key_for(config, "treatment")}
    )
    assert response.status_code == 200
    assert response.headers["X-Gateway-Variant"] == "treatment"
    assert response.headers["X-Gateway-Deployment"] == DEPLOYMENT
    assert response.headers["X-Gateway-Fallback"] == DEPLOYMENT


async def test_guardrail_stop_during_attempt_blocks_treatment_retry(fault_system, system):
    _, app, client, plans, attempts, _, _ = fault_system
    config = await configure(system, guardrail={"error_rate_max": 0, "min_requests": 1})
    key = key_for(config, "treatment")
    service = app.state.chat_service
    experiments = service._experiments

    async def fail_while_other_request_stops_experiment():
        other = RequestContext(request_id="other", session_id=key, deployment_id=SECOND)
        other.assignment = experiments.assign("qwen-7b", other, system[0].registry.snapshot)
        experiments.record(other, "error")
        raise UpstreamUnavailableError("offline")
        yield

    plans[SECOND] = [fail_while_other_request_stops_experiment]
    response = await client.post("/v1/chat", json=BODY, headers={"X-Session-Id": key})
    assert response.status_code == 200
    assert attempts == {SECOND: 1, DEPLOYMENT: 1}
    assert response.headers["X-Gateway-Variant"] == "treatment"


async def test_report_segments_request_type_and_actual_input_token_band(system):
    config = await configure(system)
    client = system[4]
    await client.post(
        "/v1/chat",
        json=BODY,
        headers={"X-Session-Id": key_for(config, "control"), "X-Request-Type": "simple_qa"},
    )
    report = (await client.get("/admin/experiments")).json()["experiments"][NAME]
    control = report["variants"]["control"]
    assert control["by_request_type"]["simple_qa"]["samples"] == 1
    assert control["by_input_token_band"]["0-1023"]["samples"] == 1


async def test_disabling_experiment_restores_static_routing(system):
    config = await configure(system)
    client = system[4]
    key = key_for(config, "treatment")
    await configure(system, enabled=False)
    response = await client.post("/v1/chat", json=BODY, headers={"X-Session-Id": key})
    assert response.headers["X-Gateway-Deployment"] == DEPLOYMENT
    assert "X-Gateway-Experiment" not in response.headers
