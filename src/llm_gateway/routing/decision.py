"""Request hints and a reusable decision for later fallback handling."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..registry.models import ModelDeployment


@dataclass(frozen=True)
class RoutingContext:
    model: str
    bucket_key: str
    bucket_source: str = "request_id"
    request_type: str | None = None
    # A character-count heuristic only; never exported as measured token usage.
    estimated_input_tokens: int | None = None
    max_tokens: int | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class RoutingDecision:
    deployment: ModelDeployment
    reason: str
    strategy: str
    alternatives: tuple[ModelDeployment, ...] = ()
