"""Request-driven guardrails: bounded observations and a latched process-local stop."""

from .models import Guardrail


def violation(report: dict, guardrail: Guardrail | None) -> str | None:
    if guardrail is None or report["samples"] < guardrail.min_requests:
        return None
    if guardrail.error_rate_max is not None and report["error_rate"] > guardrail.error_rate_max:
        return "error_rate"
    if (
        guardrail.ttft_p95_max_sec is not None
        and report["ttft_samples"] >= guardrail.min_requests
        and report["ttft_p95_sec"] > guardrail.ttft_p95_max_sec
    ):
        return "ttft_p95"
    return None
