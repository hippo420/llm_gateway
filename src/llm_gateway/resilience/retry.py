import random

from ..core.errors import GatewayError, is_retryable
from .config import RetryConfig


def can_retry(error: GatewayError, *, output_received: bool) -> bool:
    return is_retryable(error, stream_started=output_received)


def backoff_seconds(config: RetryConfig, failed_attempt: int) -> float:
    cap = min(config.initial_delay_ms * 2 ** (failed_attempt - 1), config.max_delay_ms) / 1000
    return random.uniform(0, cap) if config.jitter else cap


def breaker_failure(error: GatewayError) -> bool:
    # Bad client requests and local errors say nothing about upstream availability.
    status = error.detail.get("upstream_status")
    if isinstance(status, int) and 400 <= status < 500:
        return False
    return error.code in {
        "GW-5001",
        "GW-5002",
        "GW-5003",
        "GW-5004",
        "GW-5005",
        "GW-5006",
        "GW-5007",
        "GW-5008",
    }
