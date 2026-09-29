from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .models import PolicyState

POLICY_LOOP_ERRORS = Counter(
    "llm_gateway_policy_loop_errors_total", "Policy controller loop failures."
)


def policy_metrics(
    state: PolicyState,
    policy_ids: set[str],
    *,
    auto_configured: bool = False,
    policy_levels: dict[str, str] | None = None,
) -> bytes:
    class Collector:
        def collect(self):
            pending = GaugeMetricFamily(
                "llm_gateway_policy_pending", "Pending human approvals.", labels=["policy_id"]
            )
            active = GaugeMetricFamily(
                "llm_gateway_policy_active", "Changes under observation.", labels=["policy_id"]
            )
            conflicts = GaugeMetricFamily(
                "llm_gateway_policy_rollback_conflicts",
                "Unresolved rollback conflicts.",
                labels=["policy_id"],
            )
            decisions = CounterMetricFamily(
                "llm_gateway_recommendation",
                "Persisted human decisions and expirations.",
                labels=["policy_id", "decision"],
            )
            rollback = CounterMetricFamily(
                "llm_gateway_policy_rollback",
                "Completed conditional rollbacks.",
                labels=["policy_id"],
            )
            automatic = CounterMetricFamily(
                "llm_gateway_auto_remediation",
                "Persisted automatic applications.",
                labels=["policy_id"],
            )
            grade = GaugeMetricFamily(
                "llm_gateway_automation_level",
                "Admitted automation tier (0-3).",
                labels=["policy_id"],
            )
            enabled = GaugeMetricFamily(
                "llm_gateway_auto_remediation_enabled",
                "Effective global automation switch.",
                value=int(auto_configured and state.automation_enabled),
            )
            groups = {key: [] for key in policy_ids | {"retired"}}
            for record in state.records.values():
                groups[record.policy.id if record.policy.id in policy_ids else "retired"].append(
                    record
                )
            for name, records in groups.items():
                if name == "retired" and not records:
                    continue
                pending.add_metric([name], sum(r.status == "pending" for r in records))
                active.add_metric([name], sum(r.status == "observing" for r in records))
                conflicts.add_metric([name], sum(r.status == "rollback_conflict" for r in records))
                rollback.add_metric([name], sum(r.rolled_back for r in records))
                automatic.add_metric(
                    [name],
                    sum(
                        r.automation_level in ("L2", "L3") and r.applied_at is not None
                        for r in records
                    ),
                )
                grant = state.grants.get(name)
                ceiling = int((policy_levels or {}).get(name, "L3")[1])
                grade.add_metric([name], min(ceiling, int(grant.level[1]) if grant else 1))
                for decision in ("approved", "rejected", "expired"):
                    decisions.add_metric(
                        [name, decision],
                        sum(e["event"] == decision for r in records for e in r.events),
                    )
            yield from (pending, active, conflicts, decisions, rollback, automatic, grade, enabled)

    registry = CollectorRegistry()
    registry.register(Collector())
    return generate_latest(registry)
