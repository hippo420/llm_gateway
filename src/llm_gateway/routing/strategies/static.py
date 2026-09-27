from ...core.errors import NoAvailableDeploymentError
from ...registry.models import ModelDeployment
from ..decision import RoutingContext, RoutingDecision
from .base import RoutingStrategy


class StaticStrategy(RoutingStrategy):
    def select(
        self,
        ctx: RoutingContext,
        candidates: list[ModelDeployment],
    ) -> RoutingDecision:
        enabled = [deployment for deployment in candidates if deployment.enabled]
        if not enabled:
            raise NoAvailableDeploymentError("no enabled deployment")
        return RoutingDecision(
            deployment=enabled[0],
            reason="first_enabled",
            strategy="static",
            alternatives=tuple(enabled[1:]),
        )
