from abc import ABC, abstractmethod

from ...registry.models import ModelDeployment
from ..decision import RoutingContext, RoutingDecision


class RoutingStrategy(ABC):
    @abstractmethod
    def select(
        self,
        ctx: RoutingContext,
        candidates: list[ModelDeployment],
    ) -> RoutingDecision:
        """Choose one candidate and retain eligible alternatives in deterministic order."""
