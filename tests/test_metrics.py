"""Phase 2 계측 테스트.

prometheus_client 의 기본 REGISTRY 는 프로세스 전역이라 테스트끼리 값이 누적된다.
그래서 절대값이 아니라 **요청 전후의 차이**로 검증한다.
"""

from __future__ import annotations

from prometheus_client import REGISTRY

from llm_gateway.core.context import RequestContext
from llm_gateway.core.errors import UpstreamError
from llm_gateway.core.timing import ChatTimings
from llm_gateway.main import create_app
from llm_gateway.observability import metrics
from llm_gateway.schemas.chat import ChatCompletionRequest
from llm_gateway.service.chat_service import ChatService
from llm_gateway.settings import Settings

from .conftest import FAKE_USAGE, FakeAdapter

BODY = {
    "model": "qwen-7b",
    "messages": [{"role": "user", "content": "삼성전자 최근 실적을 분석해줘"}],
}

MODEL = "qwen-7b"
DEPLOYMENT = "qwen-7b@fake"
ADAPTER = "fake"


def _value(name: str, **labels: str) -> float:
    """아직 한 번도 기록되지 않은 시계열은 None 이 아니라 0 으로 본다."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Snapshot:
    """여러 시계열의 값을 한꺼번에 떠두고 나중에 차이를 본다."""

    def __init__(self, **series: tuple[str, dict[str, str]]) -> None:
        self._series = series
        self._before = {key: _value(name, **lbl) for key, (name, lbl) in series.items()}

    def delta(self, key: str) -> float:
        name, lbl = self._series[key]
        return _value(name, **lbl) - self._before[key]


def _requests(stream: str, status: str) -> tuple[str, dict[str, str]]:
    return (
        "llm_gateway_requests_total",
        {
            "model": MODEL,
            "deployment_id": DEPLOYMENT,
            "adapter": ADAPTER,
            "stream": stream,
            "status": status,
        },
    )


def _errors(model: str, deployment_id: str, error_type: str, code: str):
    return (
        "llm_gateway_errors_total",
        {"model": model, "deployment_id": deployment_id, "error_type": error_type, "code": code},
    )


def _md(name: str) -> tuple[str, dict[str, str]]:
    return (name, {"model": MODEL, "deployment_id": DEPLOYMENT})


def _mda(name: str) -> tuple[str, dict[str, str]]:
    return (name, {"model": MODEL, "deployment_id": DEPLOYMENT, "adapter": ADAPTER})


class TestEndpoint:
    async def test_exposes_gateway_metrics(self, client):
        await client.post("/v1/chat/completions", json=BODY)

        response = await client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "llm_gateway_requests_total" in response.text
        assert "llm_gateway_ttft_seconds_bucket" in response.text
        # *_created 시계열은 끈다 (cardinality)
        assert "llm_gateway_requests_created" not in response.text

    async def test_disabled_by_setting(self):
        import httpx

        application = create_app(Settings(api_key="", log_format="text", metrics_enabled=False))
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/metrics")

        assert response.status_code == 404


class TestSuccess:
    async def test_non_streaming_records_l1_and_l2(self, client):
        snap = Snapshot(
            requests=_requests("false", "success"),
            duration=(
                "llm_gateway_request_duration_seconds_count",
                {"model": MODEL, "deployment_id": DEPLOYMENT, "stream": "false"},
            ),
            ttft=_mda("llm_gateway_ttft_seconds_count"),
            generation=_md("llm_gateway_generation_duration_seconds_count"),
            input_total=(
                "llm_gateway_input_tokens_total",
                {"model": MODEL, "deployment_id": DEPLOYMENT, "token_source": "upstream"},
            ),
            output_total=(
                "llm_gateway_output_tokens_total",
                {"model": MODEL, "deployment_id": DEPLOYMENT, "token_source": "upstream"},
            ),
            input_hist=_md("llm_gateway_request_input_tokens_count"),
            output_hist=_md("llm_gateway_request_output_tokens_count"),
            stop=(
                "llm_gateway_finish_reason_total",
                {"model": MODEL, "deployment_id": DEPLOYMENT, "finish_reason": "stop"},
            ),
        )

        response = await client.post("/v1/chat/completions", json=BODY)

        assert response.status_code == 200
        assert snap.delta("requests") == 1
        assert snap.delta("duration") == 1
        # non-streaming 요청도 내부 streaming 집계로 TTFT 를 얻는다.
        assert snap.delta("ttft") == 1
        assert snap.delta("generation") == 1
        assert snap.delta("input_total") == FAKE_USAGE.input_tokens
        assert snap.delta("output_total") == FAKE_USAGE.output_tokens
        assert snap.delta("input_hist") == 1
        assert snap.delta("output_hist") == 1
        assert snap.delta("stop") == 1

    async def test_streaming_uses_stream_label(self, client):
        snap = Snapshot(
            stream=_requests("true", "success"),
            non_stream=_requests("false", "success"),
            ttft=_mda("llm_gateway_ttft_seconds_count"),
        )

        await client.post("/v1/chat/completions", json={**BODY, "stream": True})

        assert snap.delta("stream") == 1
        assert snap.delta("non_stream") == 0
        assert snap.delta("ttft") == 1

    async def test_queue_not_observed_when_upstream_omits_it(self, client):
        """Ollama 는 queue 시간을 주지 않는다. 0 으로 관측하면 안 된다."""
        snap = Snapshot(queue=_md("llm_gateway_queue_duration_seconds_count"))

        await client.post("/v1/chat/completions", json=BODY)

        assert snap.delta("queue") == 0

    async def test_inflight_returns_to_zero(self, client):
        await client.post("/v1/chat/completions", json=BODY)
        await client.post("/v1/chat/completions", json={**BODY, "stream": True})

        assert _value("llm_gateway_inflight_requests", **_md("x")[1]) == 0


class TestErrors:
    async def test_unknown_model_is_not_a_label(self, client):
        """클라이언트가 보낸 모델명을 label 에 넣으면 오타마다 시계열이 생긴다."""
        snap = Snapshot(errors=_errors("unknown", "none", "model_not_found", "GW-4001"))

        response = await client.post(
            "/v1/chat/completions", json={**BODY, "model": "typo-model-xyz"}
        )

        assert response.status_code == 404
        assert snap.delta("errors") == 1
        assert "typo-model-xyz" not in (await client.get("/metrics")).text

    async def test_registered_model_kept_on_pre_selection_error(self, client):
        snap = Snapshot(
            errors=_errors(MODEL, "none", "unsupported_parameter", "GW-4002"),
            requests=_requests("false", "error"),
        )

        response = await client.post("/v1/chat/completions", json={**BODY, "n": 2})

        assert response.status_code == 400
        assert snap.delta("errors") == 1
        # deployment 에 닿지 않은 요청은 requests_total 에 넣지 않는다.
        assert snap.delta("requests") == 0

    async def test_body_validation_error_counted_by_handler(self, client):
        snap = Snapshot(errors=_errors("unknown", "none", "invalid_request", "GW-4000"))

        response = await client.post("/v1/chat/completions", json={**BODY, "messages": []})

        assert response.status_code == 400
        assert snap.delta("errors") == 1

    async def test_upstream_error_counted_once(self, client, app, deployment):
        """ChatService 와 에러 핸들러가 같은 에러를 두 번 세면 안 된다."""
        app.state.adapters.register(
            deployment.id, FakeAdapter(deployment, error=UpstreamError("boom"))
        )
        snap = Snapshot(
            errors=_errors(MODEL, DEPLOYMENT, "upstream_error", "GW-5001"),
            requests=_requests("false", "error"),
            duration=(
                "llm_gateway_request_duration_seconds_count",
                {"model": MODEL, "deployment_id": DEPLOYMENT, "stream": "false"},
            ),
            finish=(
                "llm_gateway_finish_reason_total",
                {"model": MODEL, "deployment_id": DEPLOYMENT, "finish_reason": "error"},
            ),
        )

        response = await client.post("/v1/chat/completions", json=BODY)

        assert response.status_code == 502
        assert snap.delta("errors") == 1
        assert snap.delta("requests") == 1
        assert snap.delta("finish") == 1
        # 실패한 요청의 시간은 latency 분포에 섞지 않는다.
        assert snap.delta("duration") == 0

    async def test_error_mid_stream(self, client, app, deployment):
        app.state.adapters.register(
            deployment.id, FakeAdapter(deployment, error=UpstreamError("lost"))
        )
        snap = Snapshot(
            errors=_errors(MODEL, DEPLOYMENT, "upstream_error", "GW-5001"),
            requests=_requests("true", "error"),
        )

        response = await client.post("/v1/chat/completions", json={**BODY, "stream": True})

        assert response.status_code == 200
        assert snap.delta("errors") == 1
        assert snap.delta("requests") == 1
        assert _value("llm_gateway_inflight_requests", **_md("x")[1]) == 0


class TestCancelled:
    async def test_client_disconnect_mid_stream(self, registry, deployment):
        """yield 대기 중 이탈은 GeneratorExit 로 들어온다. error rate 에 섞지 않는다."""
        from llm_gateway.adapters.factory import AdapterFactory

        adapters = AdapterFactory()
        adapters.register(deployment.id, FakeAdapter(deployment))
        service = ChatService(registry, adapters)
        request = ChatCompletionRequest(**{**BODY, "stream": True})
        ctx = RequestContext(request_id="test-cancel")
        snap = Snapshot(
            cancelled=_requests("true", "cancelled"),
            error=_requests("true", "error"),
            errors=_errors(MODEL, DEPLOYMENT, "request_cancelled", "GW-4007"),
        )

        chunks = service.stream(request, ctx)
        await chunks.__anext__()  # role chunk
        await chunks.aclose()

        assert snap.delta("cancelled") == 1
        assert snap.delta("error") == 0
        assert snap.delta("errors") == 1
        assert _value("llm_gateway_inflight_requests", **_md("x")[1]) == 0


class TestRecordRequest:
    def test_none_values_are_not_observed(self):
        """모르는 값을 0 으로 관측하면 히스토그램이 왜곡된다."""
        labels = {"model": "m-none", "deployment_id": "d-none"}
        metrics.record_request(
            model="m-none",
            deployment_id="d-none",
            adapter="fake",
            stream=False,
            status=metrics.STATUS_SUCCESS,
            timings=ChatTimings(total_sec=1.0),
            input_tokens=None,
            output_tokens=None,
            token_source="upstream",
            finish_reason="stop",
        )

        assert _value("llm_gateway_request_input_tokens_count", **labels) == 0
        assert _value("llm_gateway_generation_duration_seconds_count", **labels) == 0
        assert _value(
            "llm_gateway_ttft_seconds_count", **labels, adapter="fake"
        ) == 0

    def test_output_tps_observed(self):
        labels = {"model": "m-tps", "deployment_id": "d-tps", "adapter": "fake"}
        metrics.record_request(
            model="m-tps",
            deployment_id="d-tps",
            adapter="fake",
            stream=True,
            status=metrics.STATUS_SUCCESS,
            timings=ChatTimings(total_sec=0.7, ttft_sec=0.2, generation_sec=0.5),
            input_tokens=12,
            output_tokens=5,
            token_source="upstream",
            finish_reason="stop",
        )

        assert _value("llm_gateway_output_tokens_per_second_count", **labels) == 1
        assert _value("llm_gateway_output_tokens_per_second_sum", **labels) == 10.0

    def test_tps_skipped_for_sub_millisecond_generation(self):
        """division guard: 극단적으로 짧은 생성 구간의 TPS 는 노이즈라 관측하지 않는다."""
        labels = {"model": "m-fast", "deployment_id": "d-fast", "adapter": "fake"}
        metrics.record_request(
            model="m-fast",
            deployment_id="d-fast",
            adapter="fake",
            stream=True,
            status=metrics.STATUS_SUCCESS,
            timings=ChatTimings(total_sec=0.2, ttft_sec=0.2, generation_sec=0.0001),
            input_tokens=12,
            output_tokens=5,
            token_source="upstream",
            finish_reason="stop",
        )

        assert _value("llm_gateway_output_tokens_per_second_count", **labels) == 0

    def test_unknown_finish_reason_is_bucketed(self):
        assert metrics.normalize_finish_reason("length") == "length"
        assert metrics.normalize_finish_reason("load") == "other"
