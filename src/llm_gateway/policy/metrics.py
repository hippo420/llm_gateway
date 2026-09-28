from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .models import PolicyState


def policy_metrics(state: PolicyState, policy_ids: set[str]) -> bytes:
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
                for decision in ("approved", "rejected", "expired"):
                    decisions.add_metric(
                        [name, decision],
                        sum(e["event"] == decision for r in records for e in r.events),
                    )
            yield from (pending, active, conflicts, decisions, rollback)

    registry = CollectorRegistry()
    registry.register(Collector())
    return generate_latest(registry)
