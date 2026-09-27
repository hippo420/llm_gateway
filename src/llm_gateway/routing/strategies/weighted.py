import hashlib
import json

from ...core.errors import NoAvailableDeploymentError
from ...registry.models import ModelDeployment
from ..decision import RoutingContext, RoutingDecision
from .base import RoutingStrategy


class WeightedStrategy(RoutingStrategy):
    def select(
        self,
        ctx: RoutingContext,
        candidates: list[ModelDeployment],
    ) -> RoutingDecision:
        # Canonical order makes YAML reordering and different workers agree.
        eligible = sorted(
            (d for d in candidates if d.enabled and d.weight > 0),
            key=lambda d: d.id,
        )
        total = sum(d.weight for d in eligible)
        if not total:
            raise NoAvailableDeploymentError("no enabled deployment with positive weight")
        payload = json.dumps([ctx.model, ctx.bucket_key], ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        # Exact integer arithmetic, normalized to the current positive weight sum.
        bucket = int.from_bytes(digest, "big") * total // (1 << 256)
        cumulative = 0
        for deployment in eligible:
            cumulative += deployment.weight
            if bucket < cumulative:
                return RoutingDecision(
                    deployment=deployment,
                    reason="sticky_weighted_bucket",
                    strategy="weighted",
                    alternatives=tuple(d for d in eligible if d.id != deployment.id),
                )
        raise AssertionError("weighted bucket out of range")
