import logging
from dataclasses import replace

from ...core.logging import log_event
from ...registry.models import ModelDeployment
from ..decision import RoutingContext, RoutingDecision
from ..health import HealthTracker
from .weighted import WeightedStrategy

log = logging.getLogger(__name__)


class HealthAwareStrategy(WeightedStrategy):
    def __init__(self, health: HealthTracker) -> None:
        self.health = health

    def select(self, ctx: RoutingContext, candidates: list[ModelDeployment]) -> RoutingDecision:
        eligible = []
        for deployment in candidates:
            if not deployment.enabled or deployment.weight <= 0:
                continue
            status = self.health.assess(deployment)
            if status.excluded:
                log_event(
                    log,
                    "routing_health_excluded",
                    model=ctx.model,
                    deployment_id=deployment.id,
                    reasons=list(status.reasons),
                    samples=status.samples,
                    error_rate=status.error_rate,
                    latency_p95_sec=status.latency_p95_sec,
                )
            else:
                eligible.append(deployment)
        decision = super().select(ctx, eligible)
        return replace(decision, strategy="health_aware", reason="healthy_sticky_weighted_bucket")
