"""Human approval and conditional undo through real redis-py transactions (fakeredis)."""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from llm_gateway.core.errors import ConfigError, PolicyConflictError
from llm_gateway.diagnosis.config import DiagnosisConfig
from llm_gateway.diagnosis.engine import DiagnosisEngine
from llm_gateway.diagnosis.signals import SignalSnapshot
from llm_gateway.evaluation.models import Calibration
from llm_gateway.evaluation.store import EvaluationStore
from llm_gateway.policy.engine import STATE_KEY, PolicyEngine
from llm_gateway.policy.models import Policy, PolicyConfig
from llm_gateway.registry.overrides import DeploymentOverride

from .test_diagnosis import target as target
from .test_dynamic_config import system as system
from .test_evaluation import scored_run

A, B = "qwen-7b@ollama", "qwen-7b@secondary"


def policy_data():
    return {
        "id": "shift_errors",
        "trigger_rule_id": "UPSTREAM_ANOMALY",
        "deployment_id": A,
        "actions": [
            {"type": "adjust_weight", "target": A, "delta": -20},
            {"type": "adjust_weight", "target": B, "delta": 20},
        ],
        "guard": {"cooldown_sec": 60},
        "validation": {
            "observe_sec": 300,
            "checks": [
                {
                    "deployment_id": A,
                    "conditions": [{"signal": "error_rate", "op": "<", "threshold": 0.03}],
                }
            ],
        },
        "expected_effect": "Reduce upstream errors",
    }


@pytest.fixture
async def policies(system, target, tmp_path):
    manager, path, redis, app, client = system
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    first = raw["models"]["qwen-7b"]["deployments"][0]
    first["weight"] = 60
    raw["models"]["qwen-7b"]["deployments"].append({**first, "id": B, "weight": 40})
    raw["routing"] = {"strategy": "weighted"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    await manager.reload()
    now = [datetime.now(UTC)]
    diagnosis = DiagnosisEngine(
        DiagnosisConfig(targets=[target], consecutive_matches=1, interval_sec=15), None
    )
    diagnosis.evaluate(
        {
            A: SignalSnapshot(
                at=now[0], error_rate=0.2, request_rate=1, queue_depth=0, gpu_utilization=50
            )
        },
        tick=0,
    )
    policy = Policy.model_validate(policy_data())
    controller = PolicyEngine(
        PolicyConfig(policies=[policy]), manager, diagnosis, tmp_path, clock=lambda: now[0]
    )
    app.state.policy_engine = controller
    yield SimpleNamespace(
        controller=controller,
        policy=policy,
        manager=manager,
        redis=redis,
        app=app,
        client=client,
        now=now,
        diagnosis=diagnosis,
        path=path,
    )
    await controller.close()


def weights(p):
    return [d.weight for d in p.manager.registry.all_deployments()]


async def proposed(p):
    record = await p.controller.propose(p.policy)
    assert record is not None
    return record


async def test_approval_is_required_atomic_and_persisted(policies):
    p = policies
    r = await proposed(p)
    assert weights(p) == [60, 40]
    assert await p.redis.ttl(STATE_KEY) == -1
    response = await p.client.post(
        f"/admin/recommendations/{r.id}/approve", json={"reason": "operator reviewed evidence"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "observing"
    assert weights(p) == [40, 60]
    stored = (await p.controller.state()).records[r.id]
    assert stored.approved_by.startswith("api-key:")
    assert [e["event"] for e in stored.events] == ["proposed", "approved", "applied"]
    assert "admin-secret" not in stored.model_dump_json()
    await p.manager.reload()
    assert weights(p) == [40, 60]
    restarted = PolicyEngine(
        p.controller.config, p.manager, p.diagnosis, p.controller.evaluation_path
    )
    assert (await restarted.state()).records[r.id].approved_at == stored.approved_at


async def test_rejection_keeps_config_and_reason(policies):
    p = policies
    r = await proposed(p)
    result = await p.controller.reject(r.id, "reviewer", "secondary is unhealthy")
    assert result.status == "rejected" and result.rejected_reason == "secondary is unhealthy"
    assert weights(p) == [60, 40]
    with pytest.raises(PolicyConflictError):
        await p.controller.propose(p.policy)


@pytest.mark.parametrize(
    "error_rate,request_rate,expected",
    [
        (0.01, 1, "validated"),
        (0.1, 1, "rolled_back"),
        (None, 1, "rolled_back"),
        (0.01, 0, "rolled_back"),
    ],
)
async def test_observation_pass_fail_and_missing_signals(
    policies, error_rate, request_rate, expected
):
    p = policies
    r = await proposed(p)
    await p.controller.approve(r.id, "human", "reviewed")
    p.now[0] += timedelta(seconds=331)
    p.diagnosis.last_snapshots[A] = SignalSnapshot(
        at=p.now[0], error_rate=error_rate, request_rate=request_rate
    )
    await p.controller.tick()
    record = (await p.controller.state()).records[r.id]
    assert record.status == expected
    assert weights(p) == ([40, 60] if expected == "validated" else [60, 40])
    assert record.validation_result["checks"][0]["estimated_samples"] == request_rate * 300


async def test_old_window_waits_then_rolls_back(policies):
    p = policies
    r = await proposed(p)
    await p.controller.approve(r.id, "human", "reviewed")
    p.now[0] += timedelta(seconds=300)
    await p.controller.tick()
    assert (await p.controller.state()).records[r.id].status == "observing"
    p.now[0] += timedelta(seconds=31)
    await p.controller.tick()
    assert (await p.controller.state()).records[r.id].status == "rolled_back"


async def test_manual_rollback_preserves_existing_override_and_ttl(policies):
    p = policies
    await p.manager.change_override(
        A, DeploymentOverride(timeout={"read": 90}), actor="operator", reason="slow upstream"
    )
    previous = p.manager.source.override_document
    r = await proposed(p)
    await p.controller.approve(r.id, "human", "reviewed")
    await p.controller.rollback(r.id, "human", "undo")
    current = p.manager.source.override_document
    assert current.deployments == previous.deployments
    assert current.expires_at == previous.expires_at
    assert weights(p) == [60, 40]


async def test_operator_change_blocks_undo_and_requires_resolution(policies):
    p = policies
    r = await proposed(p)
    await p.controller.approve(r.id, "human", "reviewed")
    await p.manager.change_override(
        A, DeploymentOverride(weight=45), actor="operator", reason="fix"
    )
    result = await p.controller.rollback(r.id, "human", "undo")
    assert result.status == "rollback_conflict" and not result.rolled_back
    assert weights(p) == [45, 60]
    response = await p.client.post(
        f"/admin/policy-history/{r.id}/resolve", json={"reason": "operator configuration accepted"}
    )
    assert response.status_code == 200 and response.json()["status"] == "resolved"
    assert weights(p) == [45, 60]


@pytest.mark.parametrize("change", ["yaml", "override", "stale", "expired", "policy"])
async def test_changed_evidence_rejects_approval(policies, change):
    p = policies
    r = await proposed(p)
    if change == "yaml":
        p.path.write_text(p.path.read_text().replace("weight: 60", "weight: 59"))
        await p.manager.reload()
    elif change == "override":
        await p.manager.change_override(
            A, DeploymentOverride(weight=59), actor="human", reason="fix"
        )
    elif change == "policy":
        p.controller.config_hash = "changed"
    else:
        p.now[0] += timedelta(seconds=31 if change == "stale" else 901)
    before = p.manager.registry.snapshot
    with pytest.raises(PolicyConflictError):
        await p.controller.approve(r.id, "human", "reviewed")
    assert p.manager.registry.snapshot is before


async def test_parallel_approval_applies_once(policies):
    p = policies
    r = await proposed(p)
    results = await asyncio.gather(
        p.controller.approve(r.id, "one", "reviewed"),
        p.controller.approve(r.id, "two", "reviewed"),
        return_exceptions=True,
    )
    assert sum(isinstance(x, PolicyConflictError) for x in results) == 1
    assert weights(p) == [40, 60]


async def test_flapping_active_and_hourly_guards(policies):
    p = policies
    r = await proposed(p)
    with pytest.raises(PolicyConflictError, match="pending"):
        await proposed(p)
    await p.controller.approve(r.id, "human", "reviewed")
    with pytest.raises(PolicyConflictError, match="observation"):
        await proposed(p)
    await p.controller.rollback(r.id, "human", "undo")
    with pytest.raises(PolicyConflictError, match="budget"):
        await proposed(p)


async def test_expiration_and_admin_contract_and_metrics(policies):
    p = policies
    r = await proposed(p)
    anonymous = await p.client.get("/admin/recommendations", headers={"Authorization": ""})
    assert anonymous.status_code in (401, 403)
    invalid = await p.client.post(f"/admin/recommendations/{r.id}/reject", json={"reason": " "})
    assert invalid.status_code == 400
    missing = await p.client.post("/admin/recommendations/missing/approve", json={"reason": "test"})
    assert missing.status_code == 404
    metrics = (await p.client.get("/metrics")).text
    assert 'llm_gateway_policy_pending{policy_id="shift_errors"} 1.0' in metrics
    assert r.id not in metrics
    p.now[0] += timedelta(seconds=901)
    await p.controller.tick()
    assert (await p.controller.state()).records[r.id].status == "expired"
    assert (await p.client.get("/admin/recommendations")).json()["recommendations"] == []
    assert (await p.client.get("/admin/policy-history")).json()["total"] == 1


async def test_corrupt_audit_never_overwritten(policies):
    p = policies
    await p.redis.set(STATE_KEY, "corrupt")
    with pytest.raises(ConfigError):
        await proposed(p)
    assert await p.redis.get(STATE_KEY) == "corrupt"
    assert weights(p) == [60, 40]
    assert (await p.client.get("/metrics")).status_code == 200


@pytest.mark.parametrize(
    "actions",
    [
        [{"type": "disable_deployment", "target": A}],
        [{"type": "set_timeout", "target": A, "timeout": {"read": 90}}],
    ],
)
async def test_nonweight_actions_and_inverse(policies, actions):
    p = policies
    p.policy = Policy.model_validate({**policy_data(), "actions": actions})
    p.controller.policies[p.policy.id] = p.policy
    r = await proposed(p)
    original = p.manager.registry.snapshot
    await p.controller.approve(r.id, "human", "reviewed")
    assert p.manager.registry.snapshot != original
    await p.controller.rollback(r.id, "human", "undo")
    assert p.manager.registry.snapshot == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("guard", {"require_approval": False}),
        ("guard", {"min_weight": 90, "max_weight": 10}),
        ("actions", [{"type": "adjust_weight", "target": A, "delta": 20}]),
        ("actions", [{"type": "set_timeout", "target": A, "timeout": {}}]),
        ("actions", [{"type": "limit_concurrency", "target": A}]),
        ("validation", {"observe_sec": 1, "checks": []}),
    ],
)
def test_invalid_policies_rejected(field, value):
    with pytest.raises(ValidationError):
        Policy.model_validate({**policy_data(), field: value})


@pytest.mark.parametrize("mode", ["valid", "synthetic", "uncalibrated", "changed", "expired"])
async def test_quality_gate_requires_fresh_matching_human_validated_service_data(policies, mode):
    p = policies
    run = scored_run("synthetic" if mode == "synthetic" else "service")
    run.created_at = (p.now[0] - timedelta(days=2 if mode == "expired" else 0)).isoformat()
    run.calibration = Calibration(status="not_run" if mode == "uncalibrated" else "validated")
    destination = next(d for d in p.manager.registry.all_deployments() if d.id == B)
    digest = hashlib.sha256(destination.model_dump_json().encode()).hexdigest()
    run.deployments = {B: {"config_sha256": "changed" if mode == "changed" else digest}}
    for sample in run.samples:
        sample.deployment_id = B
    store = EvaluationStore(p.controller.evaluation_path)
    store.path(run.run_id).mkdir()
    store.update(run)
    p.policy = Policy.model_validate(
        {**policy_data(), "quality": {"run_id": run.run_id, "request_type": "simple_qa"}}
    )
    p.controller.policies[p.policy.id] = p.policy
    if mode == "valid":
        r = await proposed(p)
        # Evidence is re-read on approval: revoking calibration must stop application.
        run.calibration = Calibration()
        store.update(run)
        with pytest.raises(PolicyConflictError):
            await p.controller.approve(r.id, "human", "reviewed")
    else:
        with pytest.raises(PolicyConflictError):
            await proposed(p)
    assert weights(p) == [60, 40]


@pytest.mark.parametrize("mode", ["min_weight", "static", "last_candidate", "short_ttl"])
async def test_unsafe_plans_are_rejected_without_mutation(policies, mode):
    p = policies
    if mode == "min_weight":
        p.policy = p.policy.model_copy(
            update={"guard": p.policy.guard.model_copy(update={"min_weight": 50})}
        )
    elif mode == "static":
        p.path.write_text(p.path.read_text().replace("strategy: weighted", "strategy: static"))
        await p.manager.reload()
    elif mode == "last_candidate":
        p.policy = Policy.model_validate(
            {
                **policy_data(),
                "actions": [
                    {"type": "disable_deployment", "target": A},
                    {"type": "disable_deployment", "target": B},
                ],
            }
        )
    else:
        await p.manager.change_override(
            A, DeploymentOverride(weight=60), actor="human", reason="brief change", ttl_sec=60
        )
    before = p.manager.registry.snapshot
    with pytest.raises(PolicyConflictError):
        r = await proposed(p)
        await p.controller.approve(r.id, "human", "reviewed")
    assert p.manager.registry.snapshot is before


async def test_ten_synthetic_decisions_have_complete_audit_and_metrics(policies):
    p = policies
    # Test fixture decisions are not real human approval evidence.
    p.controller.config.max_changes_per_hour = 50
    records = []
    for i in range(10):
        policy = p.policy.model_copy(update={"id": f"policy_{i}"})
        p.controller.policies[policy.id] = policy
        p.diagnosis.evaluate(
            {
                A: SignalSnapshot(
                    at=p.now[0], error_rate=0.2, request_rate=1, queue_depth=0, gpu_utilization=50
                )
            },
            tick=(i + 1) * 61,
        )
        r = await p.controller.propose(policy)
        assert r is not None
        records.append(r.id)
        if i % 2:
            await p.controller.reject(r.id, "test-reviewer", "destination needs review")
        else:
            await p.controller.approve(r.id, "test-reviewer", "synthetic fault drill")
            await p.controller.rollback(r.id, "test-reviewer", "drill finished")
        p.now[0] += timedelta(seconds=61)
    state = await p.controller.state()
    assert len(state.records) == 10
    assert sum(r.rejected_reason is not None for r in state.records.values()) == 5
    assert sum(r.rolled_back for r in state.records.values()) == 5
    assert weights(p) == [60, 40]
    assert all(state.records[key].trigger_evidence for key in records)
    metrics = (await p.client.get("/metrics")).text
    assert 'llm_gateway_policy_rollback_total{policy_id="policy_0"} 1.0' in metrics


async def test_custom_validation_window_is_rejected(policies):
    p = policies
    p.diagnosis.config.targets[0].queries = {"request_rate": "rate(requests[1h])"}
    with pytest.raises(ValueError, match="five-minute"):
        PolicyEngine(p.controller.config, p.manager, p.diagnosis, p.controller.evaluation_path)


async def test_audit_write_failure_does_not_apply_config(policies, monkeypatch):
    p = policies
    r = await proposed(p)
    before = p.manager.registry.snapshot

    async def unavailable(*args, **kwargs):
        raise RedisConnectionError("unavailable")

    monkeypatch.setattr(p.manager.source.override, "update_with_state", unavailable)
    with pytest.raises(ConfigError):
        await p.controller.approve(r.id, "human", "reviewed")
    assert p.manager.registry.snapshot is before
    assert (await p.controller.state()).records[r.id].status == "pending"


async def test_expired_lease_rolls_back_without_resurrecting_old_override(policies):
    p = policies
    r = await proposed(p)
    await p.controller.approve(r.id, "human", "reviewed")
    p.now[0] += timedelta(seconds=3601)
    # Simulate Redis TTL expiry without making the test wait one hour.
    await p.redis.delete(p.manager.source.override.KEY)
    result = await p.controller.rollback(r.id, "human", "lease expired")
    assert result.status == "rolled_back"
    assert weights(p) == [60, 40]


async def test_proposal_notification_is_emitted_after_commit(policies, caplog):
    caplog.set_level(logging.INFO)
    r = await proposed(policies)
    messages = [x for x in caplog.records if getattr(x, "event", "") == "policy_recommendation"]
    assert len(messages) == 1 and messages[0].recommendation_id == r.id
    assert (await policies.controller.state()).records[r.id].status == "pending"


async def test_rollback_refuses_changed_yaml_even_if_metrics_pass(policies):
    p = policies
    r = await proposed(p)
    await p.controller.approve(r.id, "human", "reviewed")
    p.path.write_text(p.path.read_text().replace("weight: 60", "weight: 59"))
    await p.manager.reload()
    p.now[0] += timedelta(seconds=331)
    p.diagnosis.last_snapshots[A] = SignalSnapshot(at=p.now[0], error_rate=0, request_rate=1)
    await p.controller.tick()
    result = (await p.controller.state()).records[r.id]
    assert result.status == "rollback_conflict"
    assert weights(p) == [40, 60]
