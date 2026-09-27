"""Prometheus instant queries. Missing measurements are never replaced with zero."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, fields
from datetime import datetime

import httpx


@dataclass(frozen=True)
class SignalSnapshot:
    at: datetime
    ttft_p95: float | None = None
    output_tps: float | None = None
    queue_depth: float | None = None
    gpu_utilization: float | None = None  # percent, 0..100
    gpu_memory_used_ratio: float | None = None
    input_tokens_p95: float | None = None
    error_rate: float | None = None
    active_requests: float | None = None
    request_rate: float | None = None  # completed requests / second (cold-start proxy)

    def values(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in SIGNAL_NAMES}


SIGNAL_NAMES = frozenset(f.name for f in fields(SignalSnapshot) if f.name != "at")


class PrometheusQueryError(ValueError):
    """Invalid or ambiguous query response; never silently select the first series."""


class PrometheusClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def query(self, promql: str, at: datetime) -> float | None:
        response = await self._client.get(
            "/api/v1/query", params={"query": promql, "time": at.timestamp()}
        )
        response.raise_for_status()
        try:
            body = response.json()
            if body["status"] != "success" or body.get("warnings"):
                raise PrometheusQueryError("Prometheus returned an error or partial result")
            data = body["data"]
            if data["resultType"] == "vector":
                result = data["result"]
                if not result:
                    return None
                if len(result) != 1:
                    raise PrometheusQueryError("query must return at most one series")
                value = float(result[0]["value"][1])
            elif data["resultType"] == "scalar":
                value = float(data["result"][1])
            else:
                raise PrometheusQueryError("expected scalar or instant vector")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PrometheusQueryError("invalid Prometheus instant query response") from exc
        return value if math.isfinite(value) and value >= 0 else None


def default_queries(model: str, deployment_id: str) -> dict[str, str]:
    # JSON string escaping also escapes PromQL label literals (quotes, slashes, newlines).
    labels = f"model={json.dumps(model)},deployment_id={json.dumps(deployment_id)}"

    def quantile(metric: str) -> str:
        return f"histogram_quantile(0.95, sum by (le) (rate({metric}{{{labels}}}[5m])))"

    requests = f'llm_gateway_requests_total{{{labels},status!="cancelled"}}'
    total = f"sum(rate({requests}[5m]))"
    errors = f'sum(rate(llm_gateway_requests_total{{{labels},status="error"}}[5m]))'
    return {
        "ttft_p95": quantile("llm_gateway_ttft_seconds_bucket"),
        # Sum/count from the same histogram: no mismatch between token/timing populations.
        "output_tps": (
            f"sum(rate(llm_gateway_output_tokens_per_second_sum{{{labels}}}[5m])) / "
            f"sum(rate(llm_gateway_output_tokens_per_second_count{{{labels}}}[5m]))"
        ),
        "input_tokens_p95": quantile("llm_gateway_request_input_tokens_bucket"),
        "error_rate": f"({errors} or (0 * {total})) / {total}",
        "active_requests": f"sum(llm_gateway_inflight_requests{{{labels}}})",
        "request_rate": total,
        # GPU/queue queries must be mapped explicitly to this deployment's exporter/GPU.
    }


async def collect_signals(
    client: PrometheusClient, queries: dict[str, str], at: datetime
) -> tuple[SignalSnapshot, dict[str, str]]:
    names = list(queries)
    results = await asyncio.gather(
        *(client.query(queries[name], at) for name in names), return_exceptions=True
    )
    values: dict[str, float | None] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            values[name] = None
            errors[name] = type(result).__name__
        else:
            values[name] = result
    return SignalSnapshot(at=at, **values), errors
