from ..registry.models import ModelDeployment, ModelRegistry
from ..routing.decision import RoutingDecision
from .config import ResilienceConfig


def chain(decision: RoutingDecision, config: ResilienceConfig) -> tuple[ModelDeployment, ...]:
    if not config.fallback.enabled:
        return (decision.deployment,)
    return (decision.deployment, *decision.alternatives)


def current_candidate(registry: ModelRegistry, original: ModelDeployment) -> ModelDeployment | None:
    snapshot = registry.snapshot
    model = snapshot.models.get(original.logical_model)
    if model is None:
        return None
    return next(
        (
            d
            for d in model.deployments
            if d.id == original.id
            and d.enabled
            and (snapshot.routing.strategy == "static" or d.weight > 0)
        ),
        None,
    )
