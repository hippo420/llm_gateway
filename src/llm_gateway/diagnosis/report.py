"""Serializable evidence and bounded-cardinality Grafana metrics."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from prometheus_client import Counter, Gauge

from ..core.logging import log_event
from .baseline import FixedBaseline
from .rules import Rule, Severity
from .signals import SignalSnapshot

log = logging.getLogger(__name__)
ACTIVE = Gauge(
    "llm_gateway_diagnosis_active",
    "Current confirmed diagnosis candidates (top three).",
    ["deployment_id", "rule_id", "severity"],
)
TOTAL = Counter(
    "llm_gateway_diagnosis_total",
    "Diagnosis reports after cooldown.",
    ["deployment_id", "rule_id", "severity"],
)
LAST_EVALUATION = Gauge(
    "llm_gateway_diagnosis_last_evaluation_timestamp_seconds",
    "Last diagnosis evaluation.",
)


@dataclass(frozen=True)
class Diagnosis:
    detected_at: datetime
    model: str
    deployment_id: str
    severity: Severity
    rule_id: str
    hypothesis: str
    confidence: float
    evidence: dict[str, str]
    suggested_actions: tuple[str, ...]


def make_diagnosis(
    rule: Rule,
    snapshot: SignalSnapshot,
    baseline: FixedBaseline,
    model: str,
    deployment_id: str,
) -> Diagnosis:
    values = snapshot.values()
    normalized = baseline.normalize(snapshot)
    evidence: dict[str, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        description = f"{value:.6g}"
        base = baseline.values.get(name)
        if base is not None:
            ratio = normalized[f"{name}_ratio"]
            description += f" (baseline {base:.6g}"
            description += f", x{ratio:.6g})" if ratio is not None else ", ratio undefined)"
        evidence[name] = description
    return Diagnosis(
        detected_at=snapshot.at,
        model=model,
        deployment_id=deployment_id,
        severity=rule.severity,
        rule_id=rule.id,
        hypothesis=rule.hypothesis,
        confidence=rule.confidence,
        evidence=evidence,
        suggested_actions=rule.actions,
    )


def publish(diagnosis: Diagnosis) -> None:
    TOTAL.labels(diagnosis.deployment_id, diagnosis.rule_id, diagnosis.severity).inc()
    log_event(log, "diagnosis_detected", **diagnosis.__dict__)
