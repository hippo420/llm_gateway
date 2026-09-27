"""Process-local breaker. All admission/state transitions run without awaits."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..core.logging import log_event
from ..observability.metrics import BREAKER_STATE, BREAKER_TRANSITIONS
from ..registry.models import ModelDeployment, RegistrySnapshot
from .config import BreakerConfig

log = logging.getLogger(__name__)
STATES = {"closed": 0, "half_open": 1, "open": 2}


@dataclass
class Circuit:
    identity: str
    state: str = "closed"
    failures: int = 0
    opened_at: float = 0
    generation: int = 0
    probing: bool = False


@dataclass(frozen=True)
class Permit:
    deployment_id: str
    circuit: Circuit
    generation: int


def identity(deployment: ModelDeployment) -> str:
    return deployment.model_dump_json(exclude={"enabled", "weight", "options", "timeout"})


class CircuitBreakers:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.config = BreakerConfig()
        self.circuits: dict[str, Circuit] = {}

    def sync(self, snapshot: RegistrySnapshot) -> None:
        config = snapshot.resilience.circuit_breaker
        if config != self.config:
            # A policy change invalidates outstanding permits as well as old thresholds.
            for name, circuit in self.circuits.items():
                if circuit.state != "closed":
                    self._transition(name, circuit, "closed")
                BREAKER_STATE.remove(name)
            self.circuits.clear()
            self.config = config
        deployments = {d.id: d for m in snapshot.models.values() for d in m.deployments}
        for name in set(self.circuits) - deployments.keys():
            self.circuits.pop(name)
            BREAKER_STATE.remove(name)
        for name, deployment in deployments.items():
            key = identity(deployment)
            if name not in self.circuits or self.circuits[name].identity != key:
                previous = self.circuits.get(name)
                if previous is not None and previous.state != "closed":
                    self._transition(name, previous, "closed")
                self.circuits[name] = Circuit(key)
                BREAKER_STATE.labels(name).set(0)

    def available(self, deployment: ModelDeployment) -> bool:
        if not self.config.enabled:
            return True
        circuit = self.circuits.get(deployment.id)
        if circuit is None or circuit.identity != identity(deployment):
            return False
        if circuit.state == "open":
            return self.clock() - circuit.opened_at >= self.config.cooldown_sec
        return not circuit.probing

    def acquire(self, deployment: ModelDeployment) -> Permit | None:
        if not self.available(deployment):
            return None
        circuit = self.circuits.get(deployment.id, Circuit(identity(deployment)))
        if self.config.enabled and circuit.state == "open":
            self._transition(deployment.id, circuit, "half_open")
        if self.config.enabled and circuit.state == "half_open":
            circuit.probing = True
        return Permit(deployment.id, circuit, circuit.generation)

    def finish(self, permit: Permit, outcome: bool | None) -> None:
        circuit = self.circuits.get(permit.deployment_id)
        if not self.config.enabled or circuit is not permit.circuit:
            return
        if circuit.generation != permit.generation:
            return  # Late results cannot close a circuit opened by another request.
        circuit.probing = False
        if outcome is None:
            return  # Cancellation/client errors release the probe without a health verdict.
        if outcome:
            circuit.failures = 0
            if circuit.state != "closed":
                self._transition(permit.deployment_id, circuit, "closed")
        else:
            circuit.failures += 1
            if circuit.state == "half_open" or circuit.failures >= self.config.failure_threshold:
                circuit.opened_at = self.clock()
                self._transition(permit.deployment_id, circuit, "open")

    def _transition(self, name: str, circuit: Circuit, state: str) -> None:
        circuit.state = state
        circuit.generation += 1
        BREAKER_STATE.labels(name).set(STATES[state])
        BREAKER_TRANSITIONS.labels(name, state).inc()
        log_event(log, "circuit_breaker_transition", deployment_id=name, to_state=state)
