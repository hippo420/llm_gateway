import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    at: float
    outcome: str
    ttft: float | None = None
    latency: float | None = None
    tps: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_type: str = "other"


def token_band(tokens: int | None) -> str:
    if tokens is None:
        return "unknown"
    return "0-1023" if tokens < 1024 else "1024-4095" if tokens < 4096 else "4096+"


def segmented(samples: list[Observation], minimum: int) -> dict:
    return {
        **summarize(samples, minimum),
        "by_request_type": {
            name: summarize([s for s in samples if s.request_type == name], minimum)
            for name in sorted({s.request_type for s in samples})
        },
        "by_input_token_band": {
            name: summarize([s for s in samples if token_band(s.input_tokens) == name], minimum)
            for name in sorted({token_band(s.input_tokens) for s in samples})
        },
    }


def percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[math.ceil(len(values) * fraction) - 1] if values else None


def summarize(samples: list[Observation], minimum: int) -> dict:
    measured = [s for s in samples if s.outcome not in {"warmup", "cancelled"}]
    successes = [s for s in measured if s.outcome == "success"]
    ttft = [s.ttft for s in successes if s.ttft is not None]
    latency = [s.latency for s in successes if s.latency is not None]
    tps = [s.tps for s in successes if s.tps is not None]
    errors = sum(s.outcome in {"error", "fallback"} for s in measured)
    return {
        "samples": len(measured),
        "sufficient_samples": len(measured) >= minimum,
        "outcomes": {
            name: sum(s.outcome == name for s in samples)
            for name in ("success", "error", "fallback", "cancelled", "warmup")
        },
        "error_rate": errors / len(measured) if measured else None,
        "ttft_samples": len(ttft),
        "ttft_p50_sec": percentile(ttft, 0.5),
        "ttft_p95_sec": percentile(ttft, 0.95),
        "latency_p95_sec": percentile(latency, 0.95),
        "latency_p99_sec": percentile(latency, 0.99),
        "output_tps_mean": sum(tps) / len(tps) if tps else None,
        "output_tps_p50": percentile(tps, 0.5),
        "input_tokens": sum(s.input_tokens for s in successes if s.input_tokens is not None)
        if any(s.input_tokens is not None for s in successes)
        else None,
        "output_tokens": sum(s.output_tokens for s in successes if s.output_tokens is not None)
        if any(s.output_tokens is not None for s in successes)
        else None,
    }
