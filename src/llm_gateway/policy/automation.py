"""Opt-in, evidence-gated remediation on the Phase 9 transaction/validation loop."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from redis.exceptions import RedisError

from ..core.errors import ConfigError, PolicyConflictError
from ..registry.manager import fingerprint
from .models import (
    Admission,
    AutomationGrant,
    DisableAction,
    Policy,
    PolicyState,
    Recommendation,
    ReduceWeightAction,
)

if TYPE_CHECKING:
    from ..registry.models import RegistrySnapshot
    from ..registry.overrides import OverrideDocument
    from ..resilience.breaker import CircuitBreakers
    from .engine import PolicyEngine


def policy_hash(policy: Policy) -> str:
    # Changing a tier/priority does not erase human experience of the same exact action.
    return hashlib.sha256(policy.model_dump_json(exclude={"automation"}).encode()).hexdigest()


def override_hash(document: OverrideDocument) -> str:
    return hashlib.sha256(
        json.dumps(document.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


class AutomationController:
    def __init__(self, engine: PolicyEngine, breakers: CircuitBreakers | None) -> None:
        self.engine, self.breakers = engine, breakers

    @property
    def config(self):
        return self.engine.config.auto_remediation

    @staticmethod
    def event(state: PolicyState, event: str, now: datetime, actor: str, reason: str, **data):
        state.control_events.append(
            {"event": event, "at": now.isoformat(), "actor": actor, "reason": reason, **data}
        )

    def evidence(self, state: PolicyState, policy: Policy, proof: Admission, now: datetime) -> dict:
        if (now - proof.metrics_stable_since).total_seconds() < 30 * 86400:
            raise PolicyConflictError("30 days of stable metrics are required")
        if not 0 <= (now - proof.rollback_verified_at).total_seconds() <= 30 * 86400:
            raise PolicyConflictError("a real rollback verification within 30 days is required")
        if not proof.evidence_reference.strip():
            raise PolicyConflictError("an operational evidence reference is required")
        if proof.false_positive_rate > 1 - self.config.min_agreement + 1e-9:
            raise PolicyConflictError("measured false-positive rate is too high")
        key = policy_hash(policy)
        base = self.engine.manager.source.base_snapshot
        if base is None:
            raise PolicyConflictError("base configuration is unavailable")
        records = [
            r
            for r in state.records.values()
            if policy_hash(r.policy) == key and r.base_hash == fingerprint(base)
        ]
        human = [r for r in records if r.automation_level == "L1"]
        approved = [r for r in human if r.approved_at is not None and r.approved_at <= now]
        rejected = [r for r in human if r.rejected_reason is not None]
        validated = [r for r in approved if r.status == "validated"]
        agreement = len(approved) / max(1, len(approved) + len(rejected))
        if len(validated) < self.config.min_human_decisions:
            raise PolicyConflictError(
                "30 or more validated human approvals of this policy required"
            )
        if agreement < self.config.min_agreement:
            raise PolicyConflictError("human agreement is below the automation threshold")
        failures = [
            r
            for r in records
            if r.automation_level in ("L2", "L3")
            and (r.rolled_back_at or r.status in ("rollback_conflict", "resolved"))
            and r.applied_at
            and now - r.applied_at < timedelta(days=30)
        ]
        if len(failures) >= self.config.demote_after_rollbacks:
            raise PolicyConflictError(
                "repeated recent rollback; policy is not eligible for automation"
            )
        return {"validated_human_approvals": len(validated), "agreement": agreement}

    def eligible_action(self, policy: Policy) -> None:
        if len(policy.actions) != 1 or not isinstance(
            policy.actions[0], (DisableAction, ReduceWeightAction)
        ):
            raise PolicyConflictError("automation permits one disable or reduce_weight action only")
        if policy.actions[0].target != policy.deployment_id:
            raise PolicyConflictError("automatic action must target the diagnosed deployment")

    async def set_enabled(self, enabled: bool, actor: str, reason: str) -> PolicyState:
        now = self.engine.clock()

        def change(state, document, base):
            if enabled and not self.config.enabled:
                raise PolicyConflictError(
                    "auto_remediation.enabled is false in process configuration"
                )
            state.automation_enabled = enabled
            if not enabled:
                for record in state.records.values():
                    if record.status == "pending" and record.automation_level in ("L2", "L3"):
                        record.status = "expired"
                        record.event("expired", now, actor, "automation kill switch")
            self.event(
                state,
                "automation_enabled" if enabled else "automation_disabled",
                now,
                actor,
                reason,
            )
            return document

        return await self.engine._transaction(change)

    async def admit(self, policy_id: str, proof: Admission, actor: str, reason: str) -> PolicyState:
        now = self.engine.clock()
        policy = self.engine.policies.get(policy_id)
        if policy is None or policy.automation.level not in ("L2", "L3"):
            raise PolicyConflictError("policy must be explicitly configured for L2 or L3")
        self.eligible_action(policy)

        def change(state, document, base):
            if any(r.status in ("observing", "rollback_conflict") for r in state.records.values()):
                raise PolicyConflictError("resolve active observations/conflicts before admission")
            measurements = self.evidence(state, policy, proof, now)
            state.grants[policy_id] = AutomationGrant(
                level="L2",
                policy_hash=policy_hash(policy),
                granted_at=now,
                evidence=proof,
                config_hash=self.engine.config_hash,
                base_hash=fingerprint(base),
                diagnosis_hash=hashlib.sha256(
                    self.engine.diagnosis.config.model_dump_json().encode()
                ).hexdigest(),
                initial_override_hash=override_hash(document),
            )
            # Each campaign must propose afresh with a bounded step and current evidence.
            for record in state.records.values():
                if record.policy.id == policy_id and record.status == "pending":
                    record.status = "expired"
                    record.event("expired", now, actor, "automation admission requires fresh plan")
            self.event(
                state,
                "automation_admitted",
                now,
                actor,
                reason,
                policy_id=policy_id,
                evidence=proof.model_dump(mode="json"),
                **measurements,
            )
            return document

        return await self.engine._transaction(change)

    async def demote(self, policy_id: str, actor: str, reason: str) -> PolicyState:
        now = self.engine.clock()

        def change(state, document, base):
            grant = state.grants.get(policy_id)
            if grant is None:
                raise PolicyConflictError("policy has no automation admission")
            grant.level, grant.exhausted = "L1", True
            for record in state.records.values():
                if record.policy.id == policy_id and record.status == "pending":
                    record.status = "expired"
                    record.event("expired", now, actor, "automation demoted")
            self.event(state, "automation_demoted", now, actor, reason, policy_id=policy_id)
            return document

        return await self.engine._transaction(change)

    def reduction(self, policy: Policy, grant: AutomationGrant) -> int | None:
        action = policy.actions[0]
        if isinstance(action, ReduceWeightAction):
            remaining = abs(action.delta) - grant.reduced_weight
            if remaining <= 0:
                raise PolicyConflictError("weight reduction campaign is complete")
            return min(self.config.max_weight_step, remaining)
        return None

    def guard(
        self,
        state: PolicyState,
        policy: Policy,
        document: OverrideDocument,
        base: RegistrySnapshot,
        now: datetime,
    ) -> AutomationGrant:
        if not self.config.enabled or not state.automation_enabled:
            raise PolicyConflictError("automation kill switch is off")
        grant = state.grants.get(policy.id)
        if (
            grant is None
            or grant.level not in ("L2", "L3")
            or policy.automation.level not in ("L2", "L3")
            or grant.policy_hash != policy_hash(policy)
            or grant.config_hash != self.engine.config_hash
            or grant.base_hash != fingerprint(base)
            or grant.diagnosis_hash
            != hashlib.sha256(self.engine.diagnosis.config.model_dump_json().encode()).hexdigest()
        ):
            raise PolicyConflictError("policy has no matching automation admission")
        self.eligible_action(policy)
        self.evidence(state, policy, grant.evidence, now)
        if not grant.last_record_id and grant.initial_override_hash != override_hash(document):
            raise PolicyConflictError("operator overrides changed after automation admission")
        if grant.exhausted or grant.steps >= policy.automation.max_steps:
            raise PolicyConflictError("automation campaign is complete; operator review required")
        # Global mutual exclusion is stronger than per-policy max_active.
        if any(r.status in ("observing", "rollback_conflict") for r in state.records.values()):
            raise PolicyConflictError("another change is active")
        auto = [r for r in state.records.values() if r.automation_level in ("L2", "L3")]
        changes = [at for r in auto for at in (r.applied_at, r.rolled_back_at) if at]
        for seconds, limit in (
            (3600, self.config.max_changes_per_hour),
            (86400, self.config.max_changes_per_day),
        ):
            if sum(0 <= (now - at).total_seconds() < seconds for at in changes) >= limit:
                raise PolicyConflictError("automatic change budget exhausted")
        if grant.last_record_id:
            previous = state.records[grant.last_record_id]
            if previous.status != "validated":
                raise PolicyConflictError("previous step has not been validated")
            if any(
                document.deployments.get(k) != v
                or document.expires_at.get(k) != previous.applied_expirations[k]
                or previous.applied_expirations[k] <= now
                for k, v in previous.after_overrides.items()
            ):
                raise PolicyConflictError("previous step lease changed or expired")
        if policy.automation.require_breaker_open:
            if self.breakers is None:
                raise PolicyConflictError("circuit breaker observation is unavailable")
            snapshot = self.engine.manager.source.merge(base, document)
            self.breakers.sync(snapshot)
            circuit = self.breakers.circuits.get(policy.deployment_id)
            if (
                not self.breakers.config.enabled
                or circuit is None
                or circuit.state != "open"
                or self.breakers.clock() - circuit.opened_at >= self.breakers.config.cooldown_sec
            ):
                raise PolicyConflictError("target circuit breaker is not currently OPEN")
        return grant

    def applied(self, state: PolicyState, record: Recommendation) -> None:
        grant = state.grants[record.policy.id]
        grant.steps += 1
        grant.last_record_id = record.id
        action = record.policy.actions[0]
        if isinstance(action, ReduceWeightAction):
            grant.reduced_weight += (
                record.before_config[action.target]["weight"]
                - record.after_config[action.target]["weight"]
            )
        else:
            grant.exhausted = True  # Disabling is a single-step campaign.
        if grant.steps >= record.policy.automation.max_steps or (
            isinstance(action, ReduceWeightAction) and grant.reduced_weight >= abs(action.delta)
        ):
            grant.exhausted = True

    def reconcile(self, state: PolicyState, now: datetime) -> None:
        for name, grant in state.grants.items():
            records = [
                r
                for r in state.records.values()
                if r.automation_level in ("L2", "L3") and policy_hash(r.policy) == grant.policy_hash
            ]
            if grant.last_record_id:
                latest = state.records[grant.last_record_id]
                if latest.status in ("rolled_back", "rollback_conflict", "resolved"):
                    grant.exhausted = True
            failures = [
                r
                for r in records
                if r.applied_at
                and now - r.applied_at < timedelta(days=30)
                and (r.rolled_back or r.status in ("rollback_conflict", "resolved"))
            ]
            if grant.level in ("L2", "L3") and len(failures) >= self.config.demote_after_rollbacks:
                grant.level, grant.exhausted = "L1", True
                self.event(
                    state,
                    "automation_demoted",
                    now,
                    "policy-engine",
                    "repeated automatic rollback",
                    policy_id=name,
                )
            policy = self.engine.policies.get(name)
            successes = [r for r in records if r.status == "validated" and r.applied_at]
            if (
                grant.level == "L2"
                and policy
                and policy.automation.level == "L3"
                and grant.config_hash == self.engine.config_hash
                and grant.policy_hash == policy_hash(policy)
                and not failures
                and len(successes) >= self.config.promote_after_successes
                and min(r.applied_at for r in successes if r.applied_at is not None)
                <= now - timedelta(days=30)
            ):
                try:
                    self.evidence(state, policy, grant.evidence, now)
                except PolicyConflictError:
                    continue
                grant.level = "L3"
                self.event(
                    state,
                    "automation_promoted",
                    now,
                    "policy-engine",
                    "at least 30 days and sufficient successful automatic validations",
                    policy_id=name,
                )

    async def process(self, policy: Policy) -> None:
        state = await self.engine.state()
        grant = state.grants.get(policy.id)
        automatic = (
            self.config.enabled
            and state.automation_enabled
            and grant is not None
            and grant.level in ("L2", "L3")
            and policy.automation.level in ("L2", "L3")
        )
        if not automatic:
            await self.engine.propose(policy)
            return
        pending = next(
            (
                r
                for r in state.records.values()
                if r.policy.id == policy.id
                and r.status == "pending"
                and r.automation_level in ("L2", "L3")
            ),
            None,
        )
        record = pending or await self.engine.propose(policy, automatic=True)
        if record is not None:
            await self.engine._apply(
                record.id,
                "auto-remediation",
                "admitted policy; live evidence and budgets rechecked",
                automatic=True,
            )

    async def journal(self, limit: int = 100) -> list:
        store = self.engine.manager.source.override
        assert store is not None
        try:
            rows = await store.client.xrevrange("gateway:policy:state:audit", count=limit)
            events = []
            for key, fields in rows or []:
                payload = fields.get("event", fields.get(b"event")) if fields else None
                if payload is None:
                    raise ValueError("invalid journal entry")
                events.append({"id": key, **json.loads(payload)})
            return events
        except (RedisError, ValueError, TypeError) as exc:
            raise ConfigError("automation journal is unavailable") from exc
