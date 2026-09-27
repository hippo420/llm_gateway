"""Deliberate R1-R6 reproductions and false-positive guards, without real serving/GPU."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from llm_gateway.diagnosis.baseline import FixedBaseline
from llm_gateway.diagnosis.config import DiagnosisConfig, DiagnosisTarget
from llm_gateway.diagnosis.engine import DiagnosisEngine, diagnosis_loop
from llm_gateway.diagnosis.rules import Condition, build_rules
from llm_gateway.diagnosis.signals import (
    PrometheusClient,
    PrometheusQueryError,
    SignalSnapshot,
    collect_signals,
    default_queries,
)

AT = datetime(2026, 9, 27, tzinfo=UTC)


@pytest.fixture
def target() -> DiagnosisTarget:
    # Synthetic measurements for deterministic tests, NOT operational recommendations.
    return DiagnosisTarget.model_validate(
        {
            "model": "qwen-7b",
            "deployment_id": "qwen-7b@ollama",
            "baseline": {
                "values": {
                    "ttft_p95": 1,
                    "output_tps": 40,
                    "queue_depth": 0,
                    "gpu_utilization": 50,
                    "input_tokens_p95": 1000,
                    "error_rate": 0,
                }
            },
            "thresholds": {
                "ttft_increase_ratio": 1.5,
                "ttft_extreme_ratio": 3,
                "tps_decrease_ratio": 0.7,
                "queue_increase_delta": 5,
                "input_increase_ratio": 1.5,
                "gpu_increase_ratio": 1.2,
                "gpu_saturation_percent": 90,
                "gpu_low_percent": 20,
                "memory_pressure_ratio": 0.95,
                "error_increase_delta": 0.05,
                "cold_request_rate_max": 0.01,
            },
        }
    )


@pytest.fixture
def normal() -> SignalSnapshot:
    return SignalSnapshot(
        at=AT,
        ttft_p95=1,
        output_tps=40,
        queue_depth=0,
        gpu_utilization=50,
        gpu_memory_used_ratio=0.7,
        input_tokens_p95=1000,
        error_rate=0,
        active_requests=1,
        request_rate=1,
    )


@pytest.mark.parametrize(
    ("index", "changes"),
    [
        (0, {"ttft_p95": 2, "queue_depth": 10, "gpu_utilization": 95}),
        (1, {"ttft_p95": 2, "input_tokens_p95": 2000}),
        (2, {"output_tps": 20, "gpu_utilization": 80}),
        (3, {"gpu_memory_used_ratio": 0.98, "ttft_p95": 2}),
        (3, {"gpu_memory_used_ratio": 0.98, "error_rate": 0.1}),
        (4, {"error_rate": 0.1}),
        (5, {"ttft_p95": 4, "gpu_utilization": 10, "request_rate": 0.005}),
    ],
)
def test_reproduce_each_rule(target, normal, index, changes):
    rules = build_rules(target.thresholds)
    snapshot = replace(normal, **changes)
    matched = [r.id for r in rules if r.evaluate(target.baseline.normalize(snapshot)) is True]
    expected = [rules[index].id]
    if index == 3 and "error_rate" in changes:
        expected.append("UPSTREAM_ANOMALY")
    assert matched == expected
    assert all(r.evaluate(target.baseline.normalize(normal)) is False for r in rules)


@pytest.mark.parametrize("index", [0, 1, 4, 5])
def test_missing_queue_is_not_normal(target, normal, index):
    snapshot = replace(
        normal,
        ttft_p95=4,
        input_tokens_p95=2000,
        error_rate=0.1,
        gpu_utilization=10 if index == 5 else 95 if index == 0 else 50,
        queue_depth=None,
        request_rate=0.005,
    )
    assert (
        build_rules(target.thresholds)[index].evaluate(target.baseline.normalize(snapshot)) is None
    )


def test_r4_or_branch_can_match_with_other_branch_missing(target, normal):
    rule = build_rules(target.thresholds)[3]
    snapshot = replace(normal, gpu_memory_used_ratio=0.98, ttft_p95=None, error_rate=0.1)
    assert rule.evaluate(target.baseline.normalize(snapshot)) is True
    assert rule.evaluate(target.baseline.normalize(replace(snapshot, error_rate=0))) is None


def test_zero_baseline_and_nonfinite_signals(target, normal):
    values = target.baseline.normalize(normal)
    assert values["queue_depth_ratio"] is None
    assert values["queue_depth_delta"] == 0
    assert values["error_rate_delta"] == 0
    assert Condition("ttft_p95", ">", 1).evaluate({"ttft_p95": float("nan")}) is None
    assert Condition("ttft_p95", ">", 1).evaluate({"ttft_p95": float("inf")}) is None
    with pytest.raises(ValueError):
        Condition("typo", ">", 1)


@pytest.mark.parametrize("values", [{}, {"typo": 1}, {"ttft_p95": -1}, {"ttft_p95": float("nan")}])
def test_invalid_baseline_rejected(values):
    with pytest.raises(ValidationError):
        FixedBaseline(values=values)


@pytest.mark.parametrize(
    "changes",
    [
        {"interval_sec": 16},
        {"consecutive_matches": 0},
        {"cooldown_sec": -1},
        {"query_timeout_sec": 0},
    ],
)
def test_invalid_config_rejected(changes):
    with pytest.raises(ValidationError):
        DiagnosisConfig(**changes)


def test_unknown_queries_duplicate_targets_rejected(target):
    data = target.model_dump()
    data["queries"] = {"typo": "sum(metric)"}
    with pytest.raises(ValidationError):
        DiagnosisTarget.model_validate(data)
    with pytest.raises(ValidationError):
        DiagnosisConfig(targets=[target, target])


@pytest.fixture
async def engine(target):
    client = PrometheusClient("http://prometheus")
    result = DiagnosisEngine(DiagnosisConfig(targets=[target]), client)
    yield result
    result.reset()
    await client.close()


def evaluate(engine, snapshot, tick):
    return engine.evaluate({engine.config.targets[0].deployment_id: snapshot}, tick=tick)


async def test_consecutive_cooldown_and_recovery(engine, normal):
    bad = replace(normal, ttft_p95=2, gpu_memory_used_ratio=0.99)
    assert evaluate(engine, bad, 0) == []
    assert evaluate(engine, bad, 1) == []  # duplicate poll is not another observation
    assert evaluate(engine, bad, 60) == []
    reports = evaluate(engine, bad, 120)
    assert len(reports) == 1
    assert "baseline 1, x2" in reports[0].evidence["ttft_p95"]
    assert reports[0].evidence["gpu_memory_used_ratio"] == "0.99"
    assert evaluate(engine, bad, 180) == []
    assert len(engine.current()) == 1  # cooldown suppresses reports, not current state
    for tick in (240, 300, 360):
        assert evaluate(engine, bad, tick) == []
    assert len(evaluate(engine, bad, 420)) == 1
    assert evaluate(engine, normal, 480) == []
    assert engine.current() == []


async def test_missing_sample_and_long_gap_reset_streak(engine, normal):
    bad = replace(normal, ttft_p95=2, gpu_memory_used_ratio=0.99)
    evaluate(engine, bad, 0)
    evaluate(engine, bad, 60)
    evaluate(engine, replace(bad, gpu_memory_used_ratio=None), 120)
    assert engine.rule_status["qwen-7b@ollama"]["GPU_MEMORY_PRESSURE"] == "insufficient_signal"
    assert evaluate(engine, bad, 180) == []
    assert evaluate(engine, bad, 240) == []
    assert evaluate(engine, bad, 500) == []
    assert engine.current() == []


async def test_normal_24_hours_synthetic_no_false_positives(engine, normal):
    for minute in range(24 * 60):
        assert (
            evaluate(engine, replace(normal, at=AT + timedelta(minutes=minute)), minute * 60) == []
        )


async def test_ranking_top_three_and_target_isolation(target, normal):
    second = target.model_copy(update={"deployment_id": "second"})
    client = PrometheusClient("http://prometheus")
    try:
        engine = DiagnosisEngine(
            DiagnosisConfig(targets=[target, second], consecutive_matches=1),
            client,
        )
        bad = replace(
            normal,
            ttft_p95=2,
            gpu_memory_used_ratio=0.99,
            input_tokens_p95=2000,
            output_tps=20,
            gpu_utilization=80,
        )
        results = engine.evaluate({target.deployment_id: bad, "second": bad}, tick=0)
        assert len(results) == 3
        assert [r.severity for r in results] == ["critical", "critical", "warning"]
        assert results[2].confidence == 0.75
        engine.evaluate({target.deployment_id: normal, "second": bad}, tick=60)
        assert {r.deployment_id for r in engine.current()} == {"second"}
        # A candidate not selected earlier must not acquire cooldown.
        assert engine.states["second", "CONTEXT_LENGTH_PREFILL"].last_reported == 60
    finally:
        engine.reset()
        await client.close()


def response(value):
    return {"status": "success", "data": {"resultType": "vector", "result": value}}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (response([]), None),
        (response([{"value": [0, "NaN"]}]), None),
        (response([{"value": [0, "+Inf"]}]), None),
        (response([{"value": [0, "-1"]}]), None),
        (response([{"value": [0, "0"]}]), 0),
        (response([{"value": [0, "3.5"]}]), 3.5),
        ({"status": "success", "data": {"resultType": "scalar", "result": [0, "2"]}}, 2),
    ],
)
async def test_prometheus_values(payload, expected):
    def handler(request):
        assert request.url.path == "/api/v1/query"
        assert request.url.params["time"] == str(AT.timestamp())
        return httpx.Response(200, json=payload)

    client = PrometheusClient("http://prometheus", transport=httpx.MockTransport(handler))
    try:
        assert await client.query("sum(metric)", AT) == expected
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "error"},
        {},
        response([{"value": [0, "1"]}, {"value": [0, "2"]}]),
        response([{"value": [0, "invalid"]}]),
        {"status": "success", "data": {"resultType": "matrix", "result": []}},
        {**response([]), "warnings": ["partial result"]},
    ],
)
async def test_prometheus_invalid_response(payload):
    client = PrometheusClient(
        "http://prometheus",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload),
        ),
    )
    try:
        with pytest.raises(PrometheusQueryError):
            await client.query("query", AT)
    finally:
        await client.close()


async def test_partial_query_failure_does_not_erase_healthy_signals():
    def handler(request):
        if request.url.params["query"] == "bad":
            return httpx.Response(503)
        return httpx.Response(200, json=response([{"value": [0, "2"]}]))

    client = PrometheusClient("http://prometheus", transport=httpx.MockTransport(handler))
    try:
        snapshot, errors = await collect_signals(
            client,
            {"ttft_p95": "good", "queue_depth": "bad"},
            AT,
        )
        assert snapshot.ttft_p95 == 2
        assert snapshot.queue_depth is None
        assert errors == {"queue_depth": "HTTPStatusError"}
    finally:
        await client.close()


def test_query_scope_and_no_unmapped_gpu_or_fake_queue():
    queries = default_queries('model"name', "deployment")
    assert 'model="model\\"name"' in queries["ttft_p95"]
    assert all('deployment_id="deployment"' in query for query in queries.values())
    assert "gpu_utilization" not in queries
    assert "queue_depth" not in queries
    assert 'status!="cancelled"' in queries["error_rate"]


async def test_loop_recovers_and_cancels(engine, monkeypatch):
    attempts = 0
    resumed = asyncio.Event()

    async def attempt():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        resumed.set()
        await asyncio.Future()

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(engine, "evaluate_once", attempt)
    monkeypatch.setattr("llm_gateway.diagnosis.engine.asyncio.sleep", no_sleep)
    task = asyncio.create_task(diagnosis_loop(engine))
    await asyncio.wait_for(resumed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 2
    assert engine.current() == []


async def test_admin_api_disabled_and_auth(client, app):
    assert (await client.get("/admin/diagnosis")).status_code == 401
    app.state.settings.api_key = "secret"
    assert (await client.get("/admin/diagnosis")).status_code == 401
    assert (await client.get("/admin/diagnosis/status")).status_code == 401
    assert (
        await client.get(
            "/admin/diagnosis",
            headers={
                "Authorization": "Bearer secret",
            },
        )
    ).status_code == 200
    client.headers["Authorization"] = "Bearer secret"
    assert (await client.get("/admin/diagnosis")).json() == []
    assert (await client.get("/admin/diagnosis/status")).json()["enabled"] is False


async def test_admin_api_evidence(client, app, engine, normal):
    app.state.settings.api_key = "secret"
    client.headers["Authorization"] = "Bearer secret"
    bad = replace(normal, ttft_p95=2, gpu_memory_used_ratio=0.99)
    for tick in (0, 60, 120):
        evaluate(engine, bad, tick)
    app.state.diagnosis_engine = engine
    data = (await client.get("/admin/diagnosis")).json()
    assert data[0]["rule_id"] == "GPU_MEMORY_PRESSURE"
    assert data[0]["detected_at"].endswith("Z")
    assert data[0]["evidence"]["ttft_p95"] == "2 (baseline 1, x2)"
    metrics = await client.get("/metrics")
    assert 'rule_id="GPU_MEMORY_PRESSURE",severity="critical"} 1.0' in metrics.text


async def test_evaluate_once_reports_and_records_query_failures(engine, monkeypatch):
    async def query(promql, at):
        if "ttft_seconds" in promql:
            raise httpx.ReadTimeout("timeout")
        return None

    monkeypatch.setattr(engine.client, "query", query)
    assert await engine.evaluate_once() == []
    assert engine.last_evaluated_at is not None
    assert engine.query_errors == {"qwen-7b@ollama": {"ttft_p95": "ReadTimeout"}}


async def test_lifespan_starts_and_closes_diagnosis(target, tmp_path, monkeypatch):
    import yaml

    from llm_gateway.main import create_app
    from llm_gateway.settings import Settings

    config_path = tmp_path / "diagnosis.yaml"
    config_path.write_text(yaml.safe_dump({"targets": [target.model_dump()]}), encoding="utf-8")
    closed = AsyncMock()
    monkeypatch.setattr(PrometheusClient, "close", closed)
    monkeypatch.setattr(PrometheusClient, "query", AsyncMock(return_value=None))
    application = create_app(
        Settings(
            diagnosis_enabled=True,
            diagnosis_config_path=config_path,
        )
    )
    async with application.router.lifespan_context(application):
        task = application.state.diagnosis_task
        assert task is not None
        assert application.state.diagnosis_engine is not None
        await asyncio.sleep(0)
    assert task.done()
    closed.assert_awaited_once()
    assert application.state.diagnosis_engine is None


async def test_prometheus_to_api_and_metrics(target, app, client, monkeypatch):
    app.state.settings.api_key = "secret"
    client.headers["Authorization"] = "Bearer secret"
    from llm_gateway.diagnosis.report import TOTAL

    target = target.model_copy(update={"queries": {"gpu_memory_used_ratio": "memory"}})
    config = DiagnosisConfig(targets=[target], consecutive_matches=1)

    def handler(request):
        query = request.url.params["query"]
        value = "0.99" if query == "memory" else "2" if "ttft_seconds" in query else "0"
        return httpx.Response(200, json=response([{"value": [0, value]}]))

    prometheus = PrometheusClient("http://prometheus", transport=httpx.MockTransport(handler))
    engine = DiagnosisEngine(config, prometheus)
    app.state.diagnosis_engine = engine
    counter = TOTAL.labels(target.deployment_id, "GPU_MEMORY_PRESSURE", "critical")
    previous = counter._value.get()
    try:
        reports = await engine.evaluate_once()
        assert len(reports) == 1
        assert counter._value.get() == previous + 1
        data = (await client.get("/admin/diagnosis")).json()
        assert data[0]["rule_id"] == "GPU_MEMORY_PRESSURE"
        assert data[0]["evidence"]["ttft_p95"] == "2 (baseline 1, x2)"
        status = (await client.get("/admin/diagnosis/status")).json()
        assert status["last_evaluated_at"] is not None
        assert status["query_errors"] == {}
        # HTTP outage removes the current diagnosis and breaks the consecutive streak.
        monkeypatch.setattr(
            prometheus, "query", AsyncMock(side_effect=httpx.ReadTimeout("offline"))
        )
        engine._last_tick -= config.interval_sec
        await engine.evaluate_once()
        assert (await client.get("/admin/diagnosis")).json() == []
        assert (await client.get("/admin/diagnosis/status")).json()["query_errors"]
        assert counter._value.get() == previous + 1
    finally:
        engine.reset()
        await prometheus.close()


@pytest.mark.parametrize("invalid", ["empty", "unknown", "wrong_model"])
async def test_lifespan_rejects_invalid_diagnosis_targets(target, tmp_path, monkeypatch, invalid):
    import yaml

    from llm_gateway.adapters.factory import AdapterFactory
    from llm_gateway.main import create_app
    from llm_gateway.settings import Settings

    data = target.model_dump()
    if invalid == "unknown":
        data["deployment_id"] = "unknown"
    elif invalid == "wrong_model":
        data["model"] = "wrong_model"
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "targets": [] if invalid == "empty" else [data],
            }
        ),
        encoding="utf-8",
    )
    closed = AsyncMock()
    monkeypatch.setattr(AdapterFactory, "close_all", closed)
    application = create_app(
        Settings(
            diagnosis_enabled=True,
            diagnosis_config_path=config_path,
        )
    )
    with pytest.raises(ValueError):
        async with application.router.lifespan_context(application):
            pytest.fail("invalid diagnosis config should not start")
    closed.assert_awaited_once()


def test_exact_threshold_and_zero_request_rate_do_not_fire(target, normal):
    rules = build_rules(target.thresholds)
    at_threshold = replace(
        normal, ttft_p95=1.5, queue_depth=5, gpu_utilization=90, gpu_memory_used_ratio=0.95
    )
    assert rules[0].evaluate(target.baseline.normalize(at_threshold)) is False
    assert rules[3].evaluate(target.baseline.normalize(at_threshold)) is False
    idle = replace(normal, ttft_p95=4, gpu_utilization=10, request_rate=0)
    assert rules[5].evaluate(target.baseline.normalize(idle)) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"ttft_extreme_ratio": 1.2},
        {"gpu_low_percent": 95},
    ],
)
def test_invalid_threshold_order(target, changes):
    from llm_gateway.diagnosis.rules import Thresholds

    with pytest.raises(ValidationError):
        Thresholds.model_validate(target.thresholds.model_dump() | changes)
