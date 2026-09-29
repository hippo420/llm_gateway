"""Synthetic admission histories; these are NOT real operational evidence."""

import asyncio
from datetime import timedelta

import pytest

from llm_gateway.core.errors import ConfigError, PolicyConflictError
from llm_gateway.diagnosis.signals import SignalSnapshot
from llm_gateway.policy.automation import policy_hash
from llm_gateway.policy.engine import STATE_KEY, PolicyEngine
from llm_gateway.policy.models import Admission, AutoRemediation, Policy, PolicyConfig
from llm_gateway.registry.overrides import DeploymentOverride

from .test_diagnosis import target as target
from .test_dynamic_config import system as system
from .test_policy import A, B, policy_data, weights
from .test_policy import policies as policies


def proof(p):
    return Admission(
        metrics_stable_since=p.now[0] - timedelta(days=31),
        false_positive_rate=0.01,
        rollback_verified_at=p.now[0] - timedelta(days=1),
        evidence_reference="synthetic-test-only://not-operational-evidence",
    )


@pytest.fixture
async def auto(policies):
    p = policies
    data = policy_data()
    data["actions"] = [{"type": "reduce_weight", "target": A, "delta": -30}]
    data["automation"] = {"level": "L2", "priority": 10, "max_steps": 3}
    data["guard"]["max_change_per_hour"] = 20
    # Broad threshold only for the staged-state-machine test; not a production recommendation.
    data["validation"]["checks"][0]["conditions"][0]["threshold"] = 0.3
    p.policy = Policy.model_validate(data)
    p.controller = PolicyEngine(
        PolicyConfig(
            policies=[p.policy],
            max_changes_per_hour=50,
            auto_remediation=AutoRemediation(
                enabled=True, max_changes_per_hour=10, max_changes_per_day=50
            ),
        ),
        p.manager,
        p.diagnosis,
        p.controller.evaluation_path,
        clock=lambda: p.now[0],
        breakers=p.app.state.chat_service.circuit_breakers,
    )
    p.app.state.policy_engine = p.controller
    return p


async def seed_history(p, count=30, rejects=0):
    template = await p.controller.propose(p.policy)
    assert template is not None
    state = await p.controller.state()
    old = p.now[0] - timedelta(days=2)
    state.records[template.id].status = "expired"
    state.records[template.id].created_at = old
    for i in range(count + rejects):
        record = template.model_copy(deep=True)
        record.id = f"synthetic-human-{p.policy.id}-{i}"
        record.created_at = old
        record.automation_level = "L1"
        record.events = []
        if i < count:
            record.status = "validated"
            record.approved_at = record.applied_at = old
            record.approved_by = "synthetic-test-reviewer"
            record.event("approved", old, "synthetic-test-reviewer", "test fixture only")
            record.event("validated", old, "policy-engine", "test fixture only")
        else:
            record.status, record.rejected_reason = "rejected", "synthetic disagreement"
        state.records[record.id] = record
    await p.redis.set(STATE_KEY, state.model_dump_json())


async def arm(p):
    await seed_history(p)
    await p.controller.automation.admit(p.policy.id, proof(p), "test-reviewer", "test admission")
    await p.controller.automation.set_enabled(True, "test-reviewer", "test activation")


def refresh(p, seconds=331, error=0.2):
    p.now[0] += timedelta(seconds=seconds)
    p.diagnosis.evaluate(
        {
            A: SignalSnapshot(
                at=p.now[0], error_rate=error, request_rate=1, queue_depth=0, gpu_utilization=50
            )
        },
        tick=p.now[0].timestamp(),
    )


def automatic(state):
    return [r for r in state.records.values() if r.automation_level in ("L2", "L3")]


async def test_two_switches_default_off_and_l0_observes(auto):
    p = auto
    await p.controller.tick()
    assert weights(p) == [60, 40]
    assert len(automatic(await p.controller.state())) == 0
    p.controller.config.auto_remediation.enabled = False
    with pytest.raises(PolicyConflictError):
        await p.controller.automation.set_enabled(True, "human", "test")
    p.policy.automation.level = "L0"
    await p.redis.delete(STATE_KEY, STATE_KEY + ":audit")  # independent empty fixture state
    await p.controller.tick()
    assert not (await p.controller.state()).records


@pytest.mark.parametrize(
    "mode", ["few", "disagreement", "short_metrics", "old_rollback", "false_positive"]
)
async def test_admission_requires_measured_history(auto, mode):
    p = auto
    await seed_history(
        p, count=29 if mode == "few" else 30, rejects=10 if mode == "disagreement" else 0
    )
    evidence = proof(p)
    if mode == "short_metrics":
        evidence.metrics_stable_since = p.now[0] - timedelta(days=1)
    if mode == "old_rollback":
        evidence.rollback_verified_at = p.now[0] - timedelta(days=31)
    if mode == "false_positive":
        evidence.false_positive_rate = 0.2
    with pytest.raises(PolicyConflictError):
        await p.controller.automation.admit(p.policy.id, evidence, "human", "test")
    assert not (await p.controller.state()).grants
    assert weights(p) == [60, 40]


async def test_three_bounded_steps_require_validation_and_keep_one_target(auto):
    p = auto
    await arm(p)
    await p.controller.tick()
    assert weights(p) == [50, 40]
    await p.controller.tick()
    assert weights(p) == [50, 40]
    refresh(p)
    await p.controller.tick()
    assert weights(p) == [40, 40]
    refresh(p)
    await p.controller.tick()
    assert weights(p) == [30, 40]
    refresh(p)
    await p.controller.tick()
    state = await p.controller.state()
    records = automatic(state)
    assert len(records) == 3 and all(r.status == "validated" for r in records)
    assert all(set(r.after_config) == {A} for r in records)
    assert all(r.approved_at is None and r.approved_by is None for r in records)
    assert state.grants[p.policy.id].reduced_weight == 30
    for record in records:
        assert record.before_config[A]["weight"] - record.after_config[A]["weight"] == 10
    journal = await p.controller.automation.journal(100)
    applied = [e for e in journal if e["event"] == "auto_applied"]
    assert len(applied) == 3 and all(e["trigger_evidence"] for e in applied)
    metrics = (await p.client.get("/metrics")).text
    assert 'llm_gateway_auto_remediation_total{policy_id="shift_errors"} 3.0' in metrics


async def test_kill_switch_blocks_pending_apply_and_keeps_rollback_live(auto):
    p = auto
    await arm(p)
    pending = await p.controller.propose(p.policy, automatic=True)
    response = await p.client.put(
        "/admin/automation", json={"enabled": False, "reason": "incident"}
    )
    assert response.status_code == 200 and response.json()["enabled"] is False
    with pytest.raises(PolicyConflictError):
        await p.controller._apply(pending.id, "auto-remediation", "test", automatic=True)
    assert weights(p) == [60, 40]
    # New campaign after explicit re-admission; kill must not disable ongoing validation/undo.
    refresh(p, 61)
    await p.controller.automation.admit(p.policy.id, proof(p), "human", "reviewed")
    await p.controller.automation.set_enabled(True, "human", "test")
    await p.controller.tick()
    assert weights(p) == [50, 40]
    await p.controller.automation.set_enabled(False, "human", "stop")
    refresh(p, error=0.9)
    await p.controller.tick()
    assert weights(p) == [60, 40]
    assert any(r.rolled_back for r in automatic(await p.controller.state()))


async def test_kill_rechecked_after_asynchronous_quality_fetch(auto, monkeypatch):
    p = auto
    await arm(p)
    record = await p.controller.propose(p.policy, automatic=True)
    reached, resume = asyncio.Event(), asyncio.Event()

    async def paused(*args):
        reached.set()
        await resume.wait()
        return None

    monkeypatch.setattr(p.controller, "_quality", paused)
    task = asyncio.create_task(
        p.controller._apply(record.id, "auto-remediation", "test", automatic=True)
    )
    await reached.wait()
    await p.controller.automation.set_enabled(False, "human", "kill during async fetch")
    resume.set()
    with pytest.raises(PolicyConflictError):
        await task
    assert weights(p) == [60, 40]


@pytest.mark.parametrize("budget", ["max_changes_per_hour", "max_changes_per_day"])
async def test_automatic_budget_limits_next_step_but_not_rollback(auto, budget):
    p = auto
    setattr(p.controller.config.auto_remediation, budget, 1)
    await arm(p)
    await p.controller.tick()
    refresh(p)
    await p.controller.tick()
    records = automatic(await p.controller.state())
    assert len(records) == 1 and weights(p) == [50, 40]
    await p.controller.rollback(records[0].id, "human", "undo despite exhausted budget")
    assert weights(p) == [60, 40]


async def test_human_endpoint_cannot_bypass_automatic_guards(auto):
    p = auto
    await arm(p)
    record = await p.controller.propose(p.policy, automatic=True)
    response = await p.client.post(
        f"/admin/recommendations/{record.id}/approve", json={"reason": "test"}
    )
    assert response.status_code == 409
    assert weights(p) == [60, 40]


async def test_wrong_type_journal_prevents_partial_application(auto):
    p = auto
    await arm(p)
    record = await p.controller.propose(p.policy, automatic=True)
    await p.redis.set(STATE_KEY + ":audit", "corrupt journal type")
    with pytest.raises(ConfigError):
        await p.controller._apply(record.id, "auto-remediation", "test", automatic=True)
    assert weights(p) == [60, 40]
    assert (await p.controller.state()).records[record.id].status == "pending"


async def test_operator_change_blocks_automatic_rollback(auto):
    p = auto
    await arm(p)
    await p.controller.tick()
    await p.manager.change_override(
        A, DeploymentOverride(weight=47), actor="human", reason="incident"
    )
    refresh(p, error=0.9)
    await p.controller.tick()
    state = await p.controller.state()
    assert automatic(state)[0].status == "rollback_conflict"
    assert weights(p) == [47, 40]
    assert state.grants[p.policy.id].exhausted


async def test_repeat_rollbacks_demote_and_prevent_readmission(auto):
    p = auto
    await arm(p)
    for i in range(2):
        if i:
            refresh(p, 61)
            await p.controller.automation.admit(
                p.policy.id, proof(p), "human", "new reviewed campaign"
            )
        await p.controller.tick()
        refresh(p, error=0.9)
        await p.controller.tick()
    state = await p.controller.state()
    assert state.grants[p.policy.id].level == "L1"
    assert weights(p) == [60, 40]
    with pytest.raises(PolicyConflictError):
        await p.controller.automation.admit(p.policy.id, proof(p), "human", "unsafe retry")


async def test_base_change_invalidates_admission(auto):
    p = auto
    await arm(p)
    p.path.write_text(p.path.read_text().replace("weight: 60", "weight: 59"))
    await p.manager.reload()
    await p.controller.tick()
    assert not automatic(await p.controller.state())
    assert weights(p) == [59, 40]


async def test_override_change_after_admission_requires_new_review(auto):
    p = auto
    await arm(p)
    await p.manager.change_override(
        A, DeploymentOverride(weight=55), actor="human", reason="operator intervention"
    )
    await p.controller.tick()
    assert not automatic(await p.controller.state())
    assert weights(p) == [55, 40]


async def test_multi_target_automation_is_rejected(policies):
    p = policies
    p.policy.automation.level = "L2"
    with pytest.raises(PolicyConflictError, match="one disable"):
        await p.controller.automation.admit(p.policy.id, proof(p), "human", "test")


async def test_breaker_open_required_before_single_deployment_disable(auto):
    p = auto
    import yaml

    raw = yaml.safe_load(p.path.read_text())
    raw["resilience"] = {
        "circuit_breaker": {"enabled": True, "failure_threshold": 1, "cooldown_sec": 600}
    }
    p.path.write_text(yaml.safe_dump(raw))
    await p.manager.reload()
    data = p.policy.model_dump()
    data["actions"] = [{"type": "disable_deployment", "target": A}]
    data["automation"]["require_breaker_open"] = True
    p.policy = Policy.model_validate(data)
    p.controller.policies = {p.policy.id: p.policy}
    await arm(p)
    await p.controller.tick()
    assert not automatic(await p.controller.state())
    breakers = p.controller.automation.breakers
    breakers.sync(p.manager.registry.snapshot)
    deployment = next(d for d in p.manager.registry.all_deployments() if d.id == A)
    permit = breakers.acquire(deployment)
    assert permit is not None
    breakers.finish(permit, False)
    await p.controller.tick()
    record = automatic(await p.controller.state())[0]
    assert record.after_config[A]["enabled"] is False
    assert next(d for d in p.manager.registry.all_deployments() if d.id == B).enabled


async def test_automatic_history_does_not_count_as_human_agreement(auto):
    p = auto
    await seed_history(p)
    state = await p.controller.state()
    for record in state.records.values():
        if record.approved_at:
            record.automation_level = "L2"
    await p.redis.set(STATE_KEY, state.model_dump_json())
    with pytest.raises(PolicyConflictError):
        await p.controller.automation.admit(p.policy.id, proof(p), "human", "test")


async def test_resolving_conflicts_does_not_erase_failure_history(auto):
    p = auto
    await arm(p)
    state = await p.controller.state()
    template = next(r for r in state.records.values() if r.approved_at)
    for i in range(2):
        record = template.model_copy(deep=True)
        record.id = f"resolved-auto-{i}"
        record.automation_level = "L2"
        record.approved_by = record.approved_at = None
        record.status = "resolved"
        record.applied_at = p.now[0] - timedelta(hours=2)
        state.records[record.id] = record
    await p.redis.set(STATE_KEY, state.model_dump_json())
    with pytest.raises(PolicyConflictError, match="recent rollback"):
        await p.controller.automation.admit(p.policy.id, proof(p), "human", "unsafe readmission")


async def test_l3_promotion_is_data_based_and_does_not_reset_limits(auto):
    p = auto
    p.policy.automation.level = "L3"
    await arm(p)
    state = await p.controller.state()
    template = next(r for r in state.records.values() if r.approved_at)
    for i in range(10):
        record = template.model_copy(deep=True)
        record.id = f"synthetic-auto-{i}"
        record.automation_level = "L2"
        record.approved_at = record.approved_by = None
        record.applied_at = p.now[0] - timedelta(days=31)
        state.records[record.id] = record
    grant = state.grants[p.policy.id]
    assert grant.policy_hash == policy_hash(p.policy)
    grant.steps = 3
    p.controller.automation.reconcile(state, p.now[0])
    assert grant.level == "L3" and grant.steps == 3
    assert state.control_events[-1]["event"] == "automation_promoted"


@pytest.mark.parametrize("mode", ["delete", "trim_middle"])
async def test_missing_or_truncated_audit_fails_closed(auto, mode):
    p = auto
    await arm(p)
    record = await p.controller.propose(p.policy, automatic=True)
    if mode == "delete":
        await p.redis.delete(STATE_KEY + ":audit")
    else:
        await p.redis.xdel(STATE_KEY + ":audit", "1-0")
    with pytest.raises(ConfigError, match="lost or truncated"):
        await p.controller._apply(record.id, "auto-remediation", "test", automatic=True)
    assert weights(p) == [60, 40]


async def test_priority_and_global_exclusion_prevent_competing_changes(auto):
    p = auto
    await arm(p)
    data = p.policy.model_dump()
    data["id"] = "lower_priority"
    data["automation"]["priority"] = 1
    other = Policy.model_validate(data)
    p.controller.policies[other.id] = other
    # Explicit synthetic history for the second candidate; its trigger matches simultaneously.
    saved = p.policy
    p.policy = other
    await seed_history(p)
    await p.controller.automation.admit(other.id, proof(p), "human", "test admission")
    p.policy = saved
    # Reverse insertion order to prove scheduling uses priority, not dict order.
    p.controller.policies = {other.id: other, saved.id: saved}
    await p.controller.tick()
    records = automatic(await p.controller.state())
    assert len(records) == 1 and records[0].policy.id == saved.id
    assert weights(p) == [50, 40]


async def test_concurrent_controllers_apply_one_step_only(auto):
    p = auto
    await arm(p)
    record = await p.controller.propose(p.policy, automatic=True)
    from llm_gateway.adapters.factory import AdapterFactory
    from llm_gateway.registry.loader import LayeredConfigSource, RedisConfigSource, YamlConfigSource
    from llm_gateway.registry.manager import ConfigManager
    from llm_gateway.registry.models import ModelRegistry

    source = LayeredConfigSource(
        YamlConfigSource(p.path), RedisConfigSource("redis://unused", client=p.redis)
    )
    registry = ModelRegistry(await source.read())
    adapters = AdapterFactory()
    manager = ConfigManager(source, registry, adapters)
    other = PolicyEngine(
        p.controller.config,
        manager,
        p.diagnosis,
        p.controller.evaluation_path,
        clock=lambda: p.now[0],
    )
    outcomes = await asyncio.gather(
        p.controller._apply(record.id, "auto-remediation", "test", automatic=True),
        other._apply(record.id, "auto-remediation", "test", automatic=True),
        return_exceptions=True,
    )
    assert sum(isinstance(x, PolicyConflictError) for x in outcomes) == 1
    state = await p.controller.state()
    assert state.grants[p.policy.id].steps == 1
    assert sum(e["event"] == "auto_applied" for e in await p.controller.automation.journal()) == 1
    await adapters.close_all()


async def test_automation_admin_routes_require_auth_and_journal_survives_demote(auto):
    p = auto
    assert (
        await p.client.get("/admin/automation", headers={"Authorization": ""})
    ).status_code == 401
    invalid = await p.client.put("/admin/automation", json={"enabled": "true", "reason": "test"})
    assert invalid.status_code == 400
    await seed_history(p)
    admitted = await p.client.post(
        f"/admin/automation/{p.policy.id}/admit",
        json={"reason": "test reviewed", "evidence": proof(p).model_dump(mode="json")},
    )
    assert admitted.status_code == 200 and admitted.json()["level"] == "L2"
    before = await p.controller.automation.journal(1000)
    demoted = await p.client.post(
        f"/admin/automation/{p.policy.id}/demote", json={"reason": "review"}
    )
    assert demoted.status_code == 200 and demoted.json()["level"] == "L1"
    after = (await p.client.get("/admin/automation-journal?limit=1000")).json()["events"]
    assert len(after) == len(before) + 1
    assert after[1:] == before
