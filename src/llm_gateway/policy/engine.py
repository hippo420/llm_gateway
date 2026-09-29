"""Redis-serialized approval, guarded application, observation and conditional undo."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from redis.exceptions import RedisError

from ..core.errors import ConfigError, PolicyConflictError, PolicyNotFoundError
from ..core.logging import log_event
from ..diagnosis.engine import DiagnosisEngine
from ..diagnosis.report import Diagnosis
from ..evaluation.store import EvaluationStore
from ..registry.manager import ConfigManager, fingerprint, redact
from ..registry.models import RegistrySnapshot
from ..registry.overrides import DeploymentOverride, OverrideDocument, deep_merge
from ..resilience.breaker import CircuitBreakers
from .automation import AutomationController
from .metrics import POLICY_LOOP_ERRORS
from .models import (
    DisableAction,
    Policy,
    PolicyConfig,
    PolicyState,
    Recommendation,
    ReduceWeightAction,
    TimeoutAction,
    WeightAction,
)

log = logging.getLogger(__name__)
STATE_KEY = "gateway:policy:state"
ACTIVE = {"observing", "rollback_conflict"}


def decode(raw: str | bytes | None) -> PolicyState:
    try:
        if raw is not None and len(raw) > 50_000_000:
            raise ValueError("policy state exceeds 50 MB")
        return PolicyState.model_validate_json(raw) if raw else PolicyState()
    except ValueError as exc:
        raise ConfigError("invalid policy audit state; refusing to overwrite it") from exc


def deployments(snapshot: RegistrySnapshot) -> dict:
    return {d.id: d for entry in snapshot.models.values() for d in entry.deployments}


class PolicyEngine:
    def __init__(
        self,
        config: PolicyConfig,
        manager: ConfigManager,
        diagnosis: DiagnosisEngine,
        evaluation_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        breakers: CircuitBreakers | None = None,
    ) -> None:
        self.config, self.manager, self.diagnosis = config, manager, diagnosis
        self.evaluation_path = evaluation_path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.config_hash = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
        self.policies = {p.id: p for p in config.policies if p.enabled}
        self.targets = {t.deployment_id: t for t in diagnosis.config.targets}
        if manager.source.override is None:
            raise ValueError("policies require Redis")
        for policy in self.policies.values():
            if policy.trigger_rule_id not in {
                rule.id for rule in diagnosis.rules.get(policy.deployment_id, ())
            }:
                raise ValueError("policy trigger must reference a configured Phase 3 rule")
            for check in policy.validation.checks:
                if check.deployment_id not in self.targets:
                    raise ValueError("validation requires a configured diagnosis target")
                if self.targets[check.deployment_id].queries:
                    raise ValueError("policy validation requires standard five-minute queries")
        self._task: asyncio.Task | None = None
        self.automation = AutomationController(self, breakers)

    async def state(self) -> PolicyState:
        store = self.manager.source.override
        assert store is not None
        try:
            return decode(await store.client.get(STATE_KEY))
        except RedisError as exc:
            raise ConfigError("policy history unavailable") from exc

    async def _transaction(self, fn) -> PolicyState:
        events: list[dict] = []

        def transform(raw, document, base):
            events.clear()
            state = decode(raw)
            counts = {key: len(record.events) for key, record in state.records.items()}
            control_count = len(state.control_events)
            updated = fn(state, document, base)
            if state.audit_sequence == 0:
                self.automation.event(
                    state,
                    "journal_initialized",
                    self.clock(),
                    "policy-engine",
                    "append-only journal begins; earlier Phase 9 state retained",
                    previous_state_sha256=hashlib.sha256(
                        raw.encode() if isinstance(raw, str) else raw or b""
                    ).hexdigest(),
                )
            for key, record in state.records.items():
                for event in record.events[counts.get(key, 0) :]:
                    events.append(
                        {
                            "recommendation_id": key,
                            "policy_id": record.policy.id,
                            "automation_level": record.automation_level,
                            "before_config": record.before_config,
                            "after_config": record.after_config,
                            "trigger_evidence": record.model_dump(mode="json")["trigger_evidence"],
                            "validation": record.validation_result,
                            **event,
                        }
                    )
            events.extend(state.control_events[control_count:])
            for event in events:
                state.audit_sequence += 1
                event["sequence"] = state.audit_sequence
            encoded = state.model_dump_json()
            if len(encoded.encode()) > 50_000_000:
                raise ConfigError("policy audit capacity reached; archive before further changes")
            return encoded, updated

        raw = await self.manager.atomic_override_with_state(STATE_KEY, transform, lambda: events)
        for event in events:
            log_event(log, "policy_transition", transition=event.pop("event"), **event)
        return decode(raw)

    def _fresh(self, at: datetime, now: datetime) -> bool:
        return 0 <= (now - at).total_seconds() <= self.diagnosis.config.interval_sec * 2

    def _diagnosis(self, policy: Policy, now: datetime) -> Diagnosis | None:
        return next(
            (
                d
                for d in self.diagnosis.current()
                if d.deployment_id == policy.deployment_id
                and d.rule_id == policy.trigger_rule_id
                and self._fresh(d.detected_at, now)
            ),
            None,
        )

    def _guard(self, state: PolicyState, policy: Policy, now: datetime) -> None:
        records = list(state.records.values())
        active = [r for r in records if r.status in ACTIVE]
        if len(active) >= self.config.max_active:
            raise PolicyConflictError(
                "another policy is under observation or needs rollback review"
            )
        affected = {a.target for a in policy.actions}
        if any(affected & set(r.after_overrides) for r in active):
            raise PolicyConflictError("policy targets overlap an active change")
        recent = [r for r in records if r.applied_at and now - r.applied_at < timedelta(hours=1)]
        # Rollbacks also spend the hourly change budget, but never block an undo operation.
        global_count = len(recent) + sum(
            bool(r.rolled_back_at and now - r.rolled_back_at < timedelta(hours=1)) for r in records
        )
        own = [r for r in records if r.policy.id == policy.id]
        own_count = sum(bool(r.applied_at and now - r.applied_at < timedelta(hours=1)) for r in own)
        own_count += sum(
            bool(r.rolled_back_at and now - r.rolled_back_at < timedelta(hours=1)) for r in own
        )
        if (
            global_count >= self.config.max_changes_per_hour
            or own_count >= policy.guard.max_change_per_hour
        ):
            raise PolicyConflictError("hourly policy change budget exhausted")
        changes = [
            at
            for r in records
            for at in (r.applied_at, r.rolled_back_at)
            if at and (r.policy.id == policy.id or affected & set(r.after_overrides))
        ]
        if changes and (now - max(changes)).total_seconds() < policy.guard.cooldown_sec:
            raise PolicyConflictError("policy cooldown has not elapsed")

    def _plan(
        self,
        policy: Policy,
        document: OverrideDocument,
        base: RegistrySnapshot,
        *,
        reduction: int | None = None,
    ):
        snapshot = self.manager.source.merge(base, document)
        deps = deployments(snapshot)
        names = {
            policy.deployment_id,
            *(a.target for a in policy.actions),
            *(c.deployment_id for c in policy.validation.checks),
        }
        if not names <= deps.keys():
            raise PolicyConflictError("policy references an unavailable deployment")
        model = deps[policy.deployment_id].logical_model
        if any(deps[name].logical_model != model for name in names):
            raise PolicyConflictError("policy must stay within one logical model")
        if any(
            e.enabled and any(deps[v.deployment_id].logical_model == model for v in e.variants)
            for e in snapshot.experiments.values()
        ):
            raise PolicyConflictError("pause the model's experiment before changing its policy")
        if (
            any(isinstance(a, (WeightAction, ReduceWeightAction)) for a in policy.actions)
            and snapshot.routing.strategy == "static"
        ):
            raise PolicyConflictError("weight changes require weighted or health_aware routing")
        before, after, patches = {}, {}, {}
        for action in policy.actions:
            dep = deps[action.target]
            if not dep.enabled:
                raise PolicyConflictError("policy action targets must be enabled")
            before[dep.id] = {
                "enabled": dep.enabled,
                "weight": dep.weight,
                "timeout": dep.timeout.model_dump(),
            }
            if isinstance(action, (WeightAction, ReduceWeightAction)):
                delta = (
                    -reduction
                    if isinstance(action, ReduceWeightAction) and reduction
                    else action.delta
                )
                weight = dep.weight + delta
                if not policy.guard.min_weight <= weight <= policy.guard.max_weight:
                    raise PolicyConflictError("weight change exceeds policy guard")
                patch = {"weight": weight}
            elif isinstance(action, DisableAction):
                patch = {"enabled": False}
            else:
                assert isinstance(action, TimeoutAction)
                patch = {"timeout": action.timeout.model_dump(exclude_none=True)}
            previous = document.deployments.get(dep.id)
            patches[dep.id] = DeploymentOverride.model_validate(
                deep_merge(previous.patch() if previous else {}, patch)
            )
            after[dep.id] = deep_merge(before[dep.id], patch)
            if before[dep.id] == after[dep.id]:
                raise PolicyConflictError("policy would not change the configuration")
        surviving = [
            d
            for d in deps.values()
            if d.logical_model == model
            and after.get(d.id, {}).get("enabled", d.enabled)
            and after.get(d.id, {}).get("weight", d.weight) > 0
        ]
        if not surviving:
            raise PolicyConflictError("policy would remove all positive-weight candidates")
        return snapshot, before, after, patches

    async def _quality(self, policy: Policy, now: datetime) -> dict | None:
        if policy.quality is None:
            return None
        try:
            summary = await asyncio.to_thread(
                EvaluationStore(self.evaluation_path).load_summary, policy.quality.run_id
            )
        except (ValueError, OSError) as exc:
            raise PolicyConflictError("quality evidence is unavailable") from exc
        if (
            not 0
            <= (now - datetime.fromisoformat(summary.created_at)).total_seconds()
            <= policy.quality.max_age_sec
        ):
            raise PolicyConflictError("quality evidence is expired")
        # Only validated service data can authorize traffic increases through this optional gate.
        eligible = {
            r["deployment_id"]
            for r in summary.policy_input.get("matrix", [])
            if r.get("request_type") == policy.quality.request_type and r.get("eligible")
        }
        required = {a.target for a in policy.actions if isinstance(a, WeightAction) and a.delta > 0}
        if summary.calibration.status != "validated" or not required or not required <= eligible:
            raise PolicyConflictError("quality gate has no validated eligible destination")
        if any(d.provenance != "service" for d in summary.datasets):
            raise PolicyConflictError("synthetic quality data cannot authorize a policy")
        return {name: summary.deployments.get(name, {}).get("config_sha256") for name in required}

    @staticmethod
    def _check_quality(snapshot: RegistrySnapshot, quality: dict | None) -> None:
        deps = deployments(snapshot)
        if quality and any(
            hashlib.sha256(deps[name].model_dump_json().encode()).hexdigest() != digest
            for name, digest in quality.items()
        ):
            raise PolicyConflictError("deployment differs from the quality evaluation")

    async def propose(self, policy: Policy, *, automatic: bool = False) -> Recommendation | None:
        now = self.clock()
        diagnosis = self._diagnosis(policy, now)
        if diagnosis is None:
            return None
        quality = await self._quality(policy, now)
        identifier = "rec-" + uuid4().hex

        def change(state, document, base):
            reduction = None
            level = "L1"
            if automatic:
                grant = self.automation.guard(state, policy, document, base, now)
                level = grant.level
                reduction = self.automation.reduction(policy, grant)
            self._guard(state, policy, now)
            if len(state.records) >= 10000:
                raise ConfigError("policy audit capacity reached")
            own = [r for r in state.records.values() if r.policy.id == policy.id]
            if any(r.status == "pending" for r in own):
                raise PolicyConflictError("a proposal for this policy is already pending")
            if (
                own
                and (now - max(r.created_at for r in own)).total_seconds()
                < policy.guard.cooldown_sec
            ):
                raise PolicyConflictError("recommendation cooldown has not elapsed")
            if (
                sum(r.status == "pending" for r in state.records.values())
                >= self.config.max_pending
            ):
                raise PolicyConflictError("pending recommendation limit reached")
            snapshot, before, after, patches = self._plan(
                policy, document, base, reduction=reduction
            )
            self._check_quality(snapshot, quality)
            record = Recommendation(
                id=identifier,
                policy=policy,
                policy_config_hash=self.config_hash,
                trigger=diagnosis.rule_id,
                trigger_evidence=asdict(diagnosis),
                created_at=now,
                expires_at=now + timedelta(seconds=policy.recommendation_ttl_sec),
                before_config=before,
                after_config=after,
                base_hash=fingerprint(base),
                config_hash=fingerprint(snapshot),
                before_overrides={key: document.deployments.get(key) for key in patches},
                before_expirations={key: document.expires_at.get(key) for key in patches},
                after_overrides=patches,
                automation_level=level,
            )
            record.event("proposed", now, "policy-engine", policy.expected_effect)
            state.records[identifier] = record
            return document

        state = await self._transaction(change)
        log_event(
            log,
            "policy_recommendation",
            recommendation_id=identifier,
            policy_id=policy.id,
            status="pending",
            expected_effect=policy.expected_effect,
        )
        return state.records[identifier]

    @staticmethod
    def _get(state: PolicyState, identifier: str) -> Recommendation:
        record = state.records.get(identifier)
        if record is None:
            raise PolicyNotFoundError("policy recommendation not found")
        return record

    async def approve(self, identifier: str, actor: str, reason: str) -> Recommendation:
        return await self._apply(identifier, actor, reason, automatic=False)

    async def _apply(
        self,
        identifier: str,
        actor: str,
        reason: str,
        *,
        automatic: bool,
    ) -> Recommendation:
        initial = self._get(await self.state(), identifier)
        now = self.clock()
        quality = await self._quality(initial.policy, now)

        def change(state, document, base):
            record = self._get(state, identifier)
            policy = self.policies.get(record.policy.id)
            if record.status != "pending" or now >= record.expires_at:
                raise PolicyConflictError("proposal is not pending or has expired")
            if policy is None or record.policy_config_hash != self.config_hash:
                raise PolicyConflictError("policy configuration changed; request a new proposal")
            reduction = None
            if automatic:
                grant = self.automation.guard(state, policy, document, base, now)
                if record.automation_level != grant.level:
                    raise PolicyConflictError("automation level changed since proposal")
                reduction = self.automation.reduction(policy, grant)
            elif record.automation_level in ("L2", "L3"):
                raise PolicyConflictError(
                    "automatic proposal cannot be approved as a human decision"
                )
            if self._diagnosis(policy, now) is None:
                raise PolicyConflictError("diagnosis is no longer active or is stale")
            self._guard(state, policy, now)
            snapshot, _, after, patches = self._plan(policy, document, base, reduction=reduction)
            if after != record.after_config:
                raise PolicyConflictError("planned step changed since proposal")
            if fingerprint(base) != record.base_hash or fingerprint(snapshot) != record.config_hash:
                raise PolicyConflictError(
                    "configuration changed since proposal; request a new proposal"
                )
            self._check_quality(snapshot, quality)
            for name in patches:
                if (
                    document.deployments.get(name) != record.before_overrides[name]
                    or document.expires_at.get(name) != record.before_expirations[name]
                ):
                    raise PolicyConflictError("override ownership or TTL changed since proposal")
            expirations = dict(document.expires_at)
            for name in patches:
                # Retain existing deployment TTL; never extend an operator's override.
                expiry = expirations.get(name, now + timedelta(seconds=policy.override_ttl_sec))
                if expiry <= now + timedelta(
                    seconds=policy.validation.observe_sec
                    + self.diagnosis.config.interval_sec * 2
                    + self.config.interval_sec
                ):
                    raise PolicyConflictError("override expires before observation can finish")
                expirations[name] = expiry
            record.applied_expirations = {key: expirations[key] for key in patches}
            record.status = "observing"
            record.applied_at = now
            if automatic:
                self.automation.applied(state, record)
                record.event("auto_applied", now, actor, reason)
            else:
                record.approved_by, record.approved_at = actor, now
                record.event("approved", now, actor, reason)
            record.event("applied", now, actor, reason)
            return OverrideDocument(
                updated_at=now,
                updated_by=actor,
                reason=reason,
                deployments={**document.deployments, **patches},
                expires_at=expirations,
            )

        state = await self._transaction(change)
        log_event(
            log,
            "policy_applied",
            recommendation_id=identifier,
            policy_id=initial.policy.id,
            actor=actor,
        )
        return state.records[identifier]

    async def reject(self, identifier: str, actor: str, reason: str) -> Recommendation:
        now = self.clock()

        def change(state, document, base):
            record = self._get(state, identifier)
            if record.status != "pending" or now >= record.expires_at:
                raise PolicyConflictError("only pending unexpired proposals can be rejected")
            record.status, record.rejected_by, record.rejected_reason = "rejected", actor, reason
            record.event("rejected", now, actor, reason)
            return document

        return (await self._transaction(change)).records[identifier]

    def _undo(
        self,
        record: Recommendation,
        document: OverrideDocument,
        base: RegistrySnapshot,
        now: datetime,
        actor: str,
        reason: str,
    ) -> OverrideDocument:
        if fingerprint(base) != record.base_hash:
            conflict = True
        else:
            conflict = any(
                document.deployments.get(name) != patch
                or document.expires_at.get(name) != record.applied_expirations.get(name)
                for name, patch in record.after_overrides.items()
                if record.applied_expirations[name] > now
            )
            # An operator could replace an expired lease; do not remove that new override.
            conflict |= any(
                name in document.deployments
                for name, expiry in record.applied_expirations.items()
                if expiry <= now
            )
        if conflict:
            if record.status != "rollback_conflict":
                record.event(
                    "rollback_conflict",
                    now,
                    actor,
                    "configuration changed; operator review required",
                )
            record.status = "rollback_conflict"
            return document
        patches, expirations = dict(document.deployments), dict(document.expires_at)
        for name in record.after_overrides:
            previous, expiry = record.before_overrides[name], record.before_expirations[name]
            if previous is not None and expiry is not None and expiry > now:
                patches[name], expirations[name] = previous, expiry
            else:
                patches.pop(name, None)
                expirations.pop(name, None)
        record.status, record.rolled_back, record.rolled_back_at = "rolled_back", True, now
        record.event("rolled_back", now, actor, reason)
        return OverrideDocument(
            updated_at=now,
            updated_by=actor,
            reason=reason,
            deployments=patches,
            expires_at=expirations,
        )

    async def rollback(self, identifier: str, actor: str, reason: str) -> Recommendation:
        now = self.clock()

        def change(state, document, base):
            record = self._get(state, identifier)
            if record.status not in {*ACTIVE, "validated"}:
                raise PolicyConflictError("policy has no applied change to roll back")
            return self._undo(record, document, base, now, actor, reason)

        return (await self._transaction(change)).records[identifier]

    async def resolve(self, identifier: str, actor: str, reason: str) -> Recommendation:
        """Acknowledge a reviewed rollback conflict without overwriting the operator's config."""

        def change(state, document, base):
            record = self._get(state, identifier)
            if record.status != "rollback_conflict":
                raise PolicyConflictError("only rollback conflicts can be resolved")
            record.status = "resolved"
            record.event("resolved", self.clock(), actor, reason)
            return document

        return (await self._transaction(change)).records[identifier]

    def _validation(self, record: Recommendation, now: datetime) -> dict:
        assert record.applied_at is not None
        checks = []
        for check in record.policy.validation.checks:
            snapshot = self.diagnosis.last_snapshots.get(check.deployment_id)
            target = self.targets.get(check.deployment_id)
            fresh = (
                snapshot is not None
                and self._fresh(snapshot.at, now)
                and snapshot.at
                >= record.applied_at + timedelta(seconds=record.policy.validation.observe_sec)
            )
            values = target.baseline.normalize(snapshot) if fresh and target and snapshot else {}
            samples = (values.get("request_rate") or 0) * record.policy.validation.window_sec
            result = check.rule().evaluate(values) if samples >= check.min_samples else None
            checks.append(
                {
                    "deployment_id": check.deployment_id,
                    "passed": result,
                    "estimated_samples": samples,
                    "values": values,
                    "snapshot_at": snapshot.at.isoformat() if snapshot else None,
                }
            )
        return {
            "passed": all(c["passed"] is True for c in checks),
            "checks": checks,
            "observed_sec": (now - record.applied_at).total_seconds(),
            "at": now.isoformat(),
        }

    async def tick(self) -> None:
        now = self.clock()

        def maintain(state, document, base):
            for record in state.records.values():
                if record.status == "pending" and now >= record.expires_at:
                    record.status = "expired"
                    record.event("expired", now, "policy-engine", "approval window elapsed")
                elif record.status == "observing" and record.applied_at is not None:
                    if (
                        now - record.applied_at
                    ).total_seconds() < record.policy.validation.observe_sec:
                        continue
                    record.validation_result = self._validation(record, now)
                    if (
                        any(c["passed"] is None for c in record.validation_result["checks"])
                        and (now - record.applied_at).total_seconds()
                        < record.policy.validation.observe_sec
                        + self.diagnosis.config.interval_sec * 2
                    ):
                        # Allow the next scrape to cover the full observation window.
                        continue
                    if record.validation_result["passed"]:
                        # Passing metrics cannot bless a configuration that changed behind our back.
                        owned = fingerprint(base) == record.base_hash and all(
                            document.deployments.get(k) == v
                            and document.expires_at.get(k) == record.applied_expirations[k]
                            for k, v in record.after_overrides.items()
                        )
                        if owned:
                            record.status = "validated"
                            record.event("validated", now, "policy-engine", "success criteria met")
                            continue
                    document = self._undo(
                        record,
                        document,
                        base,
                        now,
                        "policy-engine",
                        "validation failed or unavailable",
                    )
            self.automation.reconcile(state, now)
            return document

        await self._transaction(maintain)
        for policy in sorted(self.policies.values(), key=lambda p: (-p.automation.priority, p.id)):
            if policy.automation.level == "L0":
                continue
            try:
                await self.automation.process(policy)
            except PolicyConflictError:
                continue  # expected guards; visible through pending/history and unchanged config

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="policy")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                POLICY_LOOP_ERRORS.inc()
                log_event(
                    log, "policy_loop_failed", level=logging.ERROR, error_type=type(exc).__name__
                )
            await asyncio.sleep(self.config.interval_sec)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    @staticmethod
    def public(record: Recommendation) -> dict:
        return redact(record.model_dump(mode="json"))
