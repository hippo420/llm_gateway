"""Periodic evaluation, consecutive matches, current state and notification cooldown."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import DiagnosisConfig
from .report import ACTIVE, LAST_EVALUATION, Diagnosis, make_diagnosis, publish
from .rules import SEVERITY_ORDER, build_rules
from .signals import PrometheusClient, SignalSnapshot, collect_signals

log = logging.getLogger(__name__)


@dataclass
class RuleState:
    consecutive: int = 0
    last_reported: float | None = None


class DiagnosisEngine:
    def __init__(self, config: DiagnosisConfig, client: PrometheusClient) -> None:
        self.config = config
        self.client = client
        self.rules = {t.deployment_id: build_rules(t.thresholds) for t in config.targets}
        self.states = {
            (target, rule.id): RuleState() for target, rules in self.rules.items() for rule in rules
        }
        self._current: list[Diagnosis] = []
        self.last_evaluated_at: datetime | None = None
        self.query_errors: dict[str, dict[str, str]] = {}
        self.rule_status: dict[str, dict[str, str]] = {}
        self._last_tick: float | None = None
        self.reset()

    def current(self) -> list[Diagnosis]:
        return list(self._current)

    def reset(self) -> None:
        self._current = []
        self.rule_status = {}
        for state in self.states.values():
            state.consecutive = 0
        for target, rules in self.rules.items():
            for rule in rules:
                ACTIVE.labels(target, rule.id, rule.severity).set(0)

    async def evaluate_once(self) -> list[Diagnosis]:
        at = datetime.now(UTC)
        results = await asyncio.gather(
            *(
                collect_signals(self.client, target.signal_queries(), at)
                for target in self.config.targets
            )
        )
        snapshots = {
            target.deployment_id: result[0]
            for target, result in zip(self.config.targets, results, strict=True)
        }
        self.query_errors = {
            target.deployment_id: result[1]
            for target, result in zip(self.config.targets, results, strict=True)
            if result[1]
        }
        reports = self.evaluate(snapshots, tick=time.monotonic())
        self.last_evaluated_at = at
        LAST_EVALUATION.set(at.timestamp())
        for diagnosis in reports:
            publish(diagnosis)
        return reports

    def evaluate(self, snapshots: dict[str, SignalSnapshot], *, tick: float) -> list[Diagnosis]:
        # Repeated evaluations within one interval must not count as consecutive samples.
        if self._last_tick is not None:
            elapsed = tick - self._last_tick
            if elapsed < self.config.interval_sec:
                return []
            if elapsed > self.config.interval_sec * 2:
                self.reset()
        self._last_tick = tick
        candidates: list[Diagnosis] = []
        statuses: dict[str, dict[str, str]] = {}
        for target in self.config.targets:
            snapshot = snapshots.get(target.deployment_id)
            values = target.baseline.normalize(snapshot) if snapshot else {}
            statuses[target.deployment_id] = {}
            for rule in self.rules[target.deployment_id]:
                state = self.states[target.deployment_id, rule.id]
                matched = rule.evaluate(values)
                state.consecutive = state.consecutive + 1 if matched is True else 0
                statuses[target.deployment_id][rule.id] = (
                    "insufficient_signal"
                    if matched is None
                    else "normal"
                    if not matched
                    else "pending"
                    if state.consecutive < self.config.consecutive_matches
                    else "active"
                )
                ACTIVE.labels(target.deployment_id, rule.id, rule.severity).set(0)
                if state.consecutive >= self.config.consecutive_matches and snapshot is not None:
                    candidates.append(
                        make_diagnosis(
                            rule,
                            snapshot,
                            target.baseline,
                            target.model,
                            target.deployment_id,
                        )
                    )
        candidates.sort(
            key=lambda d: (-SEVERITY_ORDER[d.severity], -d.confidence, d.deployment_id, d.rule_id)
        )
        self._current = candidates[:3]
        self.rule_status = statuses
        reports: list[Diagnosis] = []
        for diagnosis in self._current:
            ACTIVE.labels(diagnosis.deployment_id, diagnosis.rule_id, diagnosis.severity).set(1)
            state = self.states[diagnosis.deployment_id, diagnosis.rule_id]
            if (
                state.last_reported is None
                or tick - state.last_reported >= self.config.cooldown_sec
            ):
                reports.append(diagnosis)
                state.last_reported = tick
        return reports


async def diagnosis_loop(engine: DiagnosisEngine) -> None:
    try:
        while True:
            try:
                await engine.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                engine.reset()
                log.exception("diagnosis evaluation failed")
            await asyncio.sleep(engine.config.interval_sec)
    finally:
        engine.reset()
