"""Select from a single configuration generation and record the decision once."""

import logging

from ..core.errors import NoAvailableDeploymentError
from ..core.logging import log_event
from ..observability.metrics import ROUTING_DECISIONS, ROUTING_NO_CANDIDATE
from ..registry.models import ModelRegistry, RegistrySnapshot
from ..resilience.breaker import CircuitBreakers
from .decision import RoutingContext, RoutingDecision
from .health import HealthTracker
from .strategies.base import RoutingStrategy
from .strategies.health_aware import HealthAwareStrategy
from .strategies.static import StaticStrategy
from .strategies.weighted import WeightedStrategy

log = logging.getLogger(__name__)


class ModelRouter:
    def __init__(
        self,
        registry: ModelRegistry,
        breakers: CircuitBreakers | None = None,
        health: HealthTracker | None = None,
    ) -> None:
        self.registry = registry
        self.breakers = breakers
        self.health = health or HealthTracker(registry)
        self.strategies: dict[str, RoutingStrategy] = {
            "static": StaticStrategy(),
            "weighted": WeightedStrategy(),
            "health_aware": HealthAwareStrategy(self.health),
        }

    def route(
        self,
        ctx: RoutingContext,
        *,
        snapshot: RegistrySnapshot | None = None,
    ) -> RoutingDecision:
        snapshot = snapshot or self.registry.snapshot
        self.health.sync(snapshot)
        try:
            candidates = self.registry.candidates(ctx.model, snapshot=snapshot)
            if self.breakers is not None:
                self.breakers.sync(snapshot)
                candidates = [d for d in candidates if self.breakers.available(d)]
            decision = self.strategies[snapshot.routing.strategy].select(ctx, candidates)
        except NoAvailableDeploymentError:
            # Only registered model names reach this branch; unknown names stay out of labels.
            ROUTING_NO_CANDIDATE.labels(ctx.model).inc()
            raise
        ROUTING_DECISIONS.labels(ctx.model, decision.deployment.id, decision.strategy).inc()
        log_event(
            log,
            "routing_decision",
            model=ctx.model,
            deployment_id=decision.deployment.id,
            strategy=decision.strategy,
            reason=decision.reason,
            alternatives=[d.id for d in decision.alternatives],
            bucket_source=ctx.bucket_source,
        )
        return decision
