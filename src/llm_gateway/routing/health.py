"""Bounded process-local attempt observations; missing evidence is never a zero metric."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from ..registry.models import ModelDeployment, ModelRegistry, RegistrySnapshot
from .config import HealthConfig


def _identity(deployment: ModelDeployment) -> str:
    # Runtime weights/enabled flags do not change the upstream being observed.
    return deployment.model_dump_json(exclude={"enabled", "weight", "options", "timeout"})


@dataclass(frozen=True)
class HealthStatus:
    samples: int = 0
    successful_samples: int = 0
    error_rate: float | None = None
    latency_p95_sec: float | None = None
    excluded: bool = False
    reasons: tuple[str, ...] = ()


class HealthTracker:
    def __init__(
        self, registry: ModelRegistry, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.config: HealthConfig | None = None
        self._snapshot: RegistrySnapshot | None = None
        self._identities: dict[str, str] = {}
        self._samples: dict[str, deque[tuple[float, bool, float]]] = {}

    def sync(self, snapshot: RegistrySnapshot) -> None:
        if snapshot is self._snapshot:
            return
        config = snapshot.routing.health_aware
        identities = {
            d.id: _identity(d) for entry in snapshot.models.values() for d in entry.deployments
        }
        if config != self.config:
            self._samples.clear()
        else:
            self._samples = {
                key: samples
                for key, samples in self._samples.items()
                if key in identities and identities[key] == self._identities.get(key)
            }
        self.config = config
        self._identities = identities
        self._snapshot = snapshot

    def observe(self, deployment: ModelDeployment, success: bool, latency_sec: float) -> None:
        self.sync(self.registry.snapshot)
        if (
            self.config is None
            or deployment.id not in self.config.thresholds
            or self._identities.get(deployment.id) != _identity(deployment)
        ):
            return
        samples = self._samples.setdefault(deployment.id, deque(maxlen=self.config.max_samples))
        samples.append((self.clock(), success, latency_sec))

    def assess(self, deployment: ModelDeployment) -> HealthStatus:
        config = self.config
        if config is None or deployment.id not in config.thresholds:
            return HealthStatus()
        samples = self._samples.get(deployment.id)
        if samples is None:
            return HealthStatus()
        cutoff = self.clock() - config.window_sec
        while samples and samples[0][0] <= cutoff:
            samples.popleft()
        successful = sorted(latency for _, success, latency in samples if success)
        error_rate = (
            (len(samples) - len(successful)) / len(samples)
            if len(samples) >= config.min_samples
            else None
        )
        latency = (
            successful[math.ceil(len(successful) * 0.95) - 1]
            if len(successful) >= config.min_samples
            else None
        )
        limits = config.thresholds[deployment.id]
        reasons = []
        if (
            error_rate is not None
            and limits.max_error_rate is not None
            and error_rate > limits.max_error_rate
        ):
            reasons.append("error_rate")
        if (
            latency is not None
            and limits.max_latency_p95_sec is not None
            and latency > limits.max_latency_p95_sec
        ):
            reasons.append("latency_p95")
        return HealthStatus(
            len(samples), len(successful), error_rate, latency, bool(reasons), tuple(reasons)
        )

    def available(self, deployment: ModelDeployment) -> bool:
        snapshot = self.registry.snapshot
        self.sync(snapshot)
        return snapshot.routing.strategy != "health_aware" or not self.assess(deployment).excluded

    def report(self) -> dict[str, Any]:
        snapshot = self.registry.snapshot
        self.sync(snapshot)
        active = snapshot.routing.strategy == "health_aware"
        deployments = {}
        for entry in snapshot.models.values():
            for deployment in entry.deployments:
                status = self.assess(deployment)
                deployments[deployment.id] = {
                    "model": entry.name,
                    **asdict(status),
                    "health_filter_admitted": not active or not status.excluded,
                }
        return {
            "strategy": snapshot.routing.strategy,
            "scope": "process",
            "health_filter_active": active,
            "config": self.config.model_dump() if self.config else None,
            "deployments": deployments,
        }
