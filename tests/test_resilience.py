"""Fault injection without a model server: policy, API boundaries and breaker admission."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from llm_gateway.adapters.base import AdapterChatChunk, AdapterUsage
from llm_gateway.adapters.factory import ADAPTER_REGISTRY
from llm_gateway.api.routes.chat import ManagedStreamingResponse
from llm_gateway.core.context import RequestContext
from llm_gateway.core.errors import (
    ContextLengthExceededError,
    InternalError,
    ModelLoadingError,
    OutOfMemoryError_,
    UpstreamConnectTimeoutError,
    UpstreamError,
    UpstreamProtocolError,
    UpstreamReadTimeoutError,
    UpstreamTotalTimeoutError,
    UpstreamUnavailableError,
)
from llm_gateway.observability.metrics import FALLBACKS, INFLIGHT, RETRIES, TIMEOUTS
from llm_gateway.registry.loader import YamlConfigSource
from llm_gateway.resilience.breaker import CircuitBreakers
from llm_gateway.resilience.config import BreakerConfig, ResilienceConfig, RetryConfig
from llm_gateway.resilience.retry import backoff_seconds
from llm_gateway.routing.decision import RoutingContext
from llm_gateway.routing.router import ModelRouter
from llm_gateway.schemas.chat import ChatCompletionRequest
from llm_gateway.service.streaming import PreparedStream

from .conftest import FakeAdapter
from .test_dynamic_config import BODY, DEPLOYMENT
from .test_dynamic_config import system as system
from .test_registry import SAMPLE

SECOND = "second"
THIRD = "third"
CONFIG = (
    SAMPLE
    + """
      - id: second
        adapter: ollama
        endpoint: http://second:11434
        upstream_model: secondary
        options: {temperature: 0.8}
      - id: third
        adapter: ollama
        endpoint: http://third:11434
        upstream_model: tertiary
resilience:
  retry: {max_attempts: 2, initial_delay_ms: 0, max_delay_ms: 0, jitter: false}
  fallback: {enabled: true, max_chain: 2}
  circuit_breaker: {enabled: false, failure_threshold: 2, cooldown_sec: 10}
"""
)


def success():
    return [
        AdapterChatChunk(delta="success"),
        AdapterChatChunk(
            finish_reason="stop",
            usage=AdapterUsage(input_tokens=3, output_tokens=2),
        ),
    ]


@pytest.fixture
async def fault_system(system, monkeypatch):
    manager, path, _, app, client = system
    plans = {}
    attempts = Counter()
    closed = Counter()
    calls = []

    class FaultAdapter(FakeAdapter):
        async def stream_chat(self, request):
            name = self.deployment.id
            attempts[name] += 1
            calls.append((name, request))
            plan = plans.get(name, [])
            action = plan.pop(0) if plan else success()
            try:
                if isinstance(action, Exception):
                    raise action
                if callable(action):
                    async for chunk in action():
                        yield chunk
                else:
                    for chunk in action:
                        if isinstance(chunk, Exception):
                            raise chunk
                        yield chunk
            finally:
                closed[name] += 1

    monkeypatch.setitem(ADAPTER_REGISTRY, "ollama", FaultAdapter)
    path.write_text(CONFIG, encoding="utf-8")
    await manager.reload()
    return manager, app, client, plans, attempts, closed, calls


@pytest.mark.parametrize("stream", [False, True])
async def test_retry_then_fallback_has_actual_headers_and_options(fault_system, stream):
    _, _, client, plans, attempts, closed, calls = fault_system
    plans[DEPLOYMENT] = [UpstreamUnavailableError("offline") for _ in range(2)]
    retry = RETRIES.labels(DEPLOYMENT, "GW-5002")
    fallback = FALLBACKS.labels(DEPLOYMENT, SECOND, "GW-5002")
    before = (retry._value.get(), fallback._value.get())
    response = await client.post("/v1/chat", json={**BODY, "stream": stream})
    assert response.status_code == 200
    assert response.headers["X-Gateway-Deployment"] == SECOND
    assert response.headers["X-Gateway-Fallback"] == SECOND
    assert attempts == {DEPLOYMENT: 2, SECOND: 1}
    assert closed == attempts
    assert calls[-1][1].model == "secondary"
    assert calls[-1][1].temperature == 0.8
    assert retry._value.get() == before[0] + 1
    assert fallback._value.get() == before[1] + 1
    if stream:
        assert response.text.endswith("data: [DONE]\n\n")
        assert response.text.count('"role":"assistant"') == 1
    else:
        assert response.json()["choices"][0]["message"]["content"] == "success"
    assert INFLIGHT.labels("qwen-7b", DEPLOYMENT)._value.get() == 0
    assert INFLIGHT.labels("qwen-7b", SECOND)._value.get() == 0


@pytest.mark.parametrize(
    "error",
    [
        UpstreamUnavailableError("offline"),
        UpstreamConnectTimeoutError("connect"),
        UpstreamReadTimeoutError("idle"),
        ModelLoadingError("loading"),
        UpstreamError("bad gateway", detail={"upstream_status": 502}),
        UpstreamError("unavailable", detail={"upstream_status": 503}),
        UpstreamError("timeout", detail={"upstream_status": 504}),
    ],
)
async def test_retryable_failure_before_output(fault_system, error):
    _, _, client, plans, attempts, _, _ = fault_system
    plans[DEPLOYMENT] = [error]
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    assert attempts == {DEPLOYMENT: 2}
    assert "X-Gateway-Fallback" not in response.headers


@pytest.mark.parametrize(
    "error",
    [
        ContextLengthExceededError("too long"),
        OutOfMemoryError_("oom"),
        UpstreamTotalTimeoutError("total"),
        UpstreamProtocolError("bad data"),
        InternalError("bug"),
        UpstreamError("400", detail={"upstream_status": 400}),
        UpstreamError("429", detail={"upstream_status": 429}),
        UpstreamError("500", detail={"upstream_status": 500}),
        ModelLoadingError("misleading loading text", detail={"upstream_status": 400}),
    ],
)
async def test_nonretryable_errors_do_not_fallback(fault_system, error):
    _, _, client, plans, attempts, closed, _ = fault_system
    plans[DEPLOYMENT] = [error]
    response = await client.post("/v1/chat/stream", json=BODY)
    assert response.status_code == error.http_status
    assert response.json()["error"]["code"] == error.code
    assert attempts == closed == {DEPLOYMENT: 1}


@pytest.mark.parametrize("stream", [False, True])
async def test_partial_output_never_replayed_in_either_api_mode(fault_system, stream):
    _, _, client, plans, attempts, closed, _ = fault_system
    plans[DEPLOYMENT] = [[AdapterChatChunk(delta="partial"), UpstreamReadTimeoutError("lost")]]
    response = await client.post("/v1/chat/completions", json={**BODY, "stream": stream})
    assert attempts == closed == {DEPLOYMENT: 1}
    if stream:
        assert response.status_code == 200
        assert response.text.count("partial") == 1
        assert "GW-5004" in response.text
        assert response.text.endswith("data: [DONE]\n\n")
    else:
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "GW-5004"


async def test_keepalives_do_not_commit_headers_or_prevent_fallback(fault_system):
    _, _, client, plans, attempts, _, _ = fault_system
    plans[DEPLOYMENT] = [
        [AdapterChatChunk(), UpstreamUnavailableError("offline")] for _ in range(2)
    ]
    response = await client.post("/v1/chat/stream", json=BODY)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Fallback"] == SECOND
    assert attempts == {DEPLOYMENT: 2, SECOND: 1}


async def test_chain_limit_and_all_failed_error_before_headers(fault_system):
    _, _, client, plans, attempts, _, _ = fault_system
    for name in (DEPLOYMENT, SECOND, THIRD):
        plans[name] = [UpstreamUnavailableError("offline") for _ in range(2)]
    response = await client.post("/v1/chat/stream", json=BODY)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "GW-5009"
    assert attempts == {DEPLOYMENT: 2, SECOND: 2}


async def test_missing_terminal_chunk_is_protocol_failure(fault_system):
    _, _, client, plans, attempts, _, _ = fault_system
    plans[DEPLOYMENT] = [[]]
    response = await client.post("/v1/chat/stream", json=BODY)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "GW-5006"
    assert attempts == {DEPLOYMENT: 1}


async def test_fallback_revalidates_disabled_candidates(fault_system):
    manager, _, client, plans, attempts, _, _ = fault_system
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_then_fail():
        entered.set()
        await release.wait()
        raise UpstreamUnavailableError("offline")
        yield  # pragma: no cover

    plans[DEPLOYMENT] = [wait_then_fail, UpstreamUnavailableError("offline")]
    task = asyncio.create_task(client.post("/v1/chat", json=BODY))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await client.put(
            f"/admin/deployments/{SECOND}", json={"enabled": False, "reason": "disable"}
        )
        release.set()
        response = await asyncio.wait_for(task, 2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert response.headers["X-Gateway-Fallback"] == THIRD
    assert attempts == {DEPLOYMENT: 2, THIRD: 1}


async def test_total_budget_includes_backoff_and_forbids_fallback(fault_system):
    manager, _, client, plans, attempts, _, _ = fault_system
    path = manager.source.base._path
    text = CONFIG.replace("read: 60", "connect: 0.01\n          read: 0.02\n          total: 0.05")
    text = text.replace(
        "initial_delay_ms: 0, max_delay_ms: 0", "initial_delay_ms: 200, max_delay_ms: 200"
    )
    path.write_text(text, encoding="utf-8")
    await manager.reload()
    plans[DEPLOYMENT] = [UpstreamUnavailableError("offline")]
    counter = TIMEOUTS.labels(DEPLOYMENT, "total")
    before = counter._value.get()
    response = await client.post("/v1/chat", json=BODY)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "GW-5005"
    assert attempts == {DEPLOYMENT: 1}
    assert counter._value.get() == before + 1


async def test_total_timeout_closes_stalled_adapter(fault_system):
    manager, _, client, plans, attempts, closed, _ = fault_system
    manager.source.base._path.write_text(
        CONFIG.replace(
            "read: 60",
            "connect: 0.01\n          read: 0.02\n          total: 0.05",
        ),
        encoding="utf-8",
    )
    await manager.reload()

    async def stall():
        await asyncio.Event().wait()
        yield

    plans[DEPLOYMENT] = [stall]
    response = await asyncio.wait_for(client.post("/v1/chat/stream", json=BODY), 2)
    assert response.status_code == 504
    assert attempts == closed == {DEPLOYMENT: 1}


async def test_cancellation_while_priming_closes_adapter_without_retry(fault_system):
    _, _, client, plans, attempts, closed, _ = fault_system
    entered = asyncio.Event()

    async def stall():
        entered.set()
        await asyncio.Event().wait()
        yield

    plans[DEPLOYMENT] = [stall]
    task = asyncio.create_task(client.post("/v1/chat/stream", json=BODY))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == closed == {DEPLOYMENT: 1}
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "chat-stream" and not t.done()]


async def test_response_disconnect_closes_bounded_producer(fault_system):
    _, app, _, plans, attempts, closed, _ = fault_system

    async def endless():
        while True:
            yield AdapterChatChunk(delta="x")
            await asyncio.sleep(0)

    plans[DEPLOYMENT] = [endless]
    ctx = RequestContext(request_id="disconnect")
    stream = await PreparedStream.open(
        app.state.chat_service.stream(
            ChatCompletionRequest.model_validate(BODY),
            ctx,
        )
    )
    response = ManagedStreamingResponse(stream, ctx)

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    async def receive():
        await asyncio.Event().wait()

    from starlette.requests import ClientDisconnect

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert stream.task.done()
    assert attempts == closed == {DEPLOYMENT: 1}
    assert INFLIGHT.labels("qwen-7b", DEPLOYMENT)._value.get() == 0


def test_exponential_backoff_cap_and_jitter():
    config = RetryConfig(initial_delay_ms=100, max_delay_ms=250, jitter=False)
    assert [backoff_seconds(config, i) for i in range(1, 5)] == [0.1, 0.2, 0.25, 0.25]
    jitter = config.model_copy(update={"jitter": True})
    assert all(0 <= backoff_seconds(jitter, 4) <= 0.25 for _ in range(100))


@pytest.mark.parametrize(
    "replacement",
    [
        "retry: {max_attempts: 0}",
        "retry: {max_attempts: 6}",
        "retry: {retry_on_stream_started: true}",
        "retry: {jitter: 1}",
        "retry: {backoff: linear}",
        "retry: {initial_delay_ms: 10, max_delay_ms: 1}",
        "fallback: {max_chain: 0}",
        "circuit_breaker: {failure_threshold: 0}",
        "circuit_breaker: {cooldown_sec: .nan}",
        "unknown: true",
    ],
)
def test_invalid_resilience_config_rejected(tmp_path, replacement):
    path = tmp_path / "gateway.yaml"
    path.write_text(SAMPLE + "\nresilience:\n  " + replacement + "\n", encoding="utf-8")
    from llm_gateway.core.errors import ConfigError

    with pytest.raises(ConfigError):
        YamlConfigSource(path).load()


def configured_breaker(registry):
    config = ResilienceConfig(
        circuit_breaker=BreakerConfig(
            enabled=True,
            failure_threshold=2,
            cooldown_sec=10,
        )
    )
    registry.swap(registry.snapshot.model_copy(update={"resilience": config}))
    now = [0.0]
    breakers = CircuitBreakers(clock=lambda: now[0])
    breakers.sync(registry.snapshot)
    return breakers, now


def test_breaker_open_single_probe_recovery_and_stale_completion(registry, deployment):
    breakers, now = configured_breaker(registry)
    late = breakers.acquire(deployment)
    for _ in range(2):
        breakers.finish(breakers.acquire(deployment), False)
    assert breakers.circuits[deployment.id].state == "open"
    assert not breakers.available(deployment)
    breakers.finish(late, True)
    assert not breakers.available(deployment)
    now[0] = 10
    probe = breakers.acquire(deployment)
    assert breakers.circuits[deployment.id].state == "half_open"
    assert breakers.acquire(deployment) is None
    breakers.finish(probe, True)
    assert breakers.circuits[deployment.id].state == "closed"
    assert breakers.available(deployment)


def test_failed_probe_reopens_and_cancelled_probe_is_released(registry, deployment):
    breakers, now = configured_breaker(registry)
    for _ in range(2):
        breakers.finish(breakers.acquire(deployment), False)
    now[0] = 10
    breakers.finish(breakers.acquire(deployment), None)
    assert breakers.available(deployment)
    breakers.finish(breakers.acquire(deployment), False)
    assert not breakers.available(deployment)
    now[0] = 20
    assert breakers.available(deployment)


def test_router_excludes_open_and_endpoint_reload_resets_breaker(registry, deployment):
    breakers, _ = configured_breaker(registry)
    for _ in range(2):
        breakers.finish(breakers.acquire(deployment), False)
    from llm_gateway.core.errors import NoAvailableDeploymentError

    router = ModelRouter(registry, breakers)
    with pytest.raises(NoAvailableDeploymentError):
        router.route(RoutingContext("qwen-7b", "session"))
    raw = registry.snapshot.model_dump()
    raw["models"]["qwen-7b"]["deployments"][0]["endpoint"] = "http://replacement:11434"
    registry.swap(type(registry.snapshot).model_validate(raw))
    assert router.route(RoutingContext("qwen-7b", "session")).deployment.endpoint.endswith(":11434")
    assert breakers.circuits[deployment.id].state == "closed"


async def test_breaker_trips_skips_retry_and_routes_next_request_elsewhere(fault_system):
    manager, app, client, plans, attempts, _, _ = fault_system
    text = CONFIG.replace(
        "enabled: false, failure_threshold: 2", "enabled: true, failure_threshold: 1"
    )
    manager.source.base._path.write_text(text, encoding="utf-8")
    await manager.reload()
    plans[DEPLOYMENT] = [UpstreamUnavailableError("offline")]
    response = await client.post("/v1/chat", json=BODY)
    assert response.headers["X-Gateway-Fallback"] == SECOND
    assert app.state.chat_service._breakers.circuits[DEPLOYMENT].state == "open"
    response = await client.post("/v1/chat", json=BODY)
    assert response.headers["X-Gateway-Deployment"] == SECOND
    assert "X-Gateway-Fallback" not in response.headers
    assert attempts == {DEPLOYMENT: 1, SECOND: 2}


async def test_upstream_self_cancellation_does_not_hang_stream_priming(fault_system):
    _, _, client, plans, attempts, closed, _ = fault_system

    async def cancel():
        raise asyncio.CancelledError
        yield

    plans[DEPLOYMENT] = [cancel]
    response = await asyncio.wait_for(client.post("/v1/chat/stream", json=BODY), 2)
    assert response.status_code == 499
    assert attempts == closed == {DEPLOYMENT: 1}


@pytest.mark.parametrize("stream", [False, True])
async def test_real_asgi_disconnect_before_headers_cancels_work(fault_system, stream):
    import json

    _, app, _, plans, attempts, closed, _ = fault_system
    entered = asyncio.Event()

    async def stall():
        entered.set()
        await asyncio.Event().wait()
        yield

    plans[DEPLOYMENT] = [stall]
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request",
                "body": json.dumps({**BODY, "stream": stream}).encode(),
                "more_body": False,
            }
        await entered.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat",
        "raw_path": b"/v1/chat",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
        ],
        "client": ("127.0.0.1", 10000),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), 2)
    assert attempts == closed == {DEPLOYMENT: 1}
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "chat-stream" and not t.done()]


async def test_live_half_open_allows_only_one_probe(fault_system):
    manager, app, client, plans, attempts, _, _ = fault_system
    manager.source.base._path.write_text(
        CONFIG.replace(
            "enabled: false, failure_threshold: 2",
            "enabled: true, failure_threshold: 1",
        ),
        encoding="utf-8",
    )
    await manager.reload()
    breaker = app.state.chat_service._breakers
    now = [0.0]
    breaker.clock = lambda: now[0]
    plans[DEPLOYMENT] = [UpstreamUnavailableError("offline")]
    await client.post("/v1/chat", json=BODY)
    now[0] = 10
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe():
        entered.set()
        await release.wait()
        for chunk in success():
            yield chunk

    plans[DEPLOYMENT] = [probe]
    task = asyncio.create_task(client.post("/v1/chat", json=BODY))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await client.post("/v1/chat", json=BODY)
        assert response.headers["X-Gateway-Deployment"] == SECOND
        assert attempts[DEPLOYMENT] == 2  # One failure, exactly one recovery probe.
        release.set()
        assert (await asyncio.wait_for(task, 2)).status_code == 200
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert breaker.circuits[DEPLOYMENT].state == "closed"


async def test_invalid_policy_reload_preserves_current_config(fault_system):
    manager, _, client, _, _, _, _ = fault_system
    previous = manager.registry.snapshot
    manager.source.base._path.write_text(
        CONFIG.replace("jitter: false", "jitter: false, retry_on_stream_started: true"),
        encoding="utf-8",
    )
    response = await client.post("/admin/config/reload")
    assert response.status_code == 500
    assert manager.registry.snapshot is previous
    assert (await client.post("/v1/chat", json=BODY)).status_code == 200


async def test_fallback_shares_initial_total_budget(fault_system):
    manager, _, client, plans, attempts, closed, _ = fault_system
    text = CONFIG.replace("max_attempts: 2", "max_attempts: 1").replace(
        "read: 60",
        "connect: 0.01\n          read: 0.1\n          total: 0.15",
    )
    manager.source.base._path.write_text(text, encoding="utf-8")
    await manager.reload()

    async def slow_failure():
        await asyncio.sleep(0.05)
        raise UpstreamUnavailableError("offline")
        yield

    async def stall():
        await asyncio.Event().wait()
        yield

    plans[DEPLOYMENT] = [slow_failure]
    plans[SECOND] = [stall]
    response = await asyncio.wait_for(client.post("/v1/chat", json=BODY), 1)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "GW-5005"
    assert attempts == closed == {DEPLOYMENT: 1, SECOND: 1}


async def test_client_error_does_not_open_enabled_breaker(fault_system):
    manager, app, client, plans, attempts, _, _ = fault_system
    manager.source.base._path.write_text(
        CONFIG.replace(
            "enabled: false, failure_threshold: 2",
            "enabled: true, failure_threshold: 1",
        ),
        encoding="utf-8",
    )
    await manager.reload()
    plans[DEPLOYMENT] = [UpstreamError("bad input", detail={"upstream_status": 400})]
    assert (await client.post("/v1/chat", json=BODY)).status_code == 502
    circuit = app.state.chat_service._breakers.circuits[DEPLOYMENT]
    assert circuit.state == "closed" and circuit.failures == 0
    assert (await client.post("/v1/chat", json=BODY)).status_code == 200
    assert attempts == {DEPLOYMENT: 2}


async def test_cancellation_during_backoff_does_not_start_another_attempt(fault_system):
    manager, _, client, plans, attempts, closed, _ = fault_system
    manager.source.base._path.write_text(
        CONFIG.replace(
            "initial_delay_ms: 0, max_delay_ms: 0",
            "initial_delay_ms: 1000, max_delay_ms: 1000",
        ),
        encoding="utf-8",
    )
    await manager.reload()
    failed = asyncio.Event()

    async def failure():
        failed.set()
        raise UpstreamUnavailableError("offline")
        yield

    plans[DEPLOYMENT] = [failure]
    task = asyncio.create_task(client.post("/v1/chat", json=BODY))
    await asyncio.wait_for(failed.wait(), 2)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == closed == {DEPLOYMENT: 1}


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("blocked_send", [False, True])
async def test_disconnect_after_headers_interrupts_stalled_upstream(
    fault_system, spec_version, blocked_send
):
    _, app, _, plans, attempts, closed, _ = fault_system
    stalled = asyncio.Event()
    content_sent = asyncio.Event()

    async def partial_then_stall():
        yield AdapterChatChunk(delta="partial")
        stalled.set()
        await asyncio.Event().wait()

    plans[DEPLOYMENT] = [partial_then_stall]
    ctx = RequestContext(request_id="disconnect-after-headers")
    service = app.state.chat_service
    request = ChatCompletionRequest.model_validate({**BODY, "stream": True})
    stream = await PreparedStream.open(service.stream(request, ctx))
    response = ManagedStreamingResponse(stream, ctx)
    sent = []

    async def send(message):
        sent.append(message)
        if b"partial" in message.get("body", b""):
            content_sent.set()
            if blocked_send:
                await asyncio.Event().wait()

    async def receive():
        await content_sent.wait()
        await stalled.wait()
        return {"type": "http.disconnect"}

    await asyncio.wait_for(
        response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send),
        1,
    )
    assert attempts == closed == {DEPLOYMENT: 1}
    assert stream.task.done()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in {"chat-send", "chat-disconnect"} and not task.done()
    ]
    assert INFLIGHT.labels("qwen-7b", DEPLOYMENT)._value.get() == 0
    assert not any(b"[DONE]" in message.get("body", b"") for message in sent)
