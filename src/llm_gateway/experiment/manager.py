from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.context import RequestContext
from ..core.logging import log_event
from ..core.timing import ChatTimings
from ..observability import metrics
from ..registry.models import ModelRegistry, RegistrySnapshot
from .assignment import Assignment, choose
from .guardrail import violation
from .models import Experiment
from .report import Observation, segmented, summarize

log = logging.getLogger(__name__)


@dataclass
class Run:
    config: Experiment
    generation: str
    model: str
    paused: bool = False
    reason: str | None = None
    assigned: Counter = field(default_factory=Counter)
    samples: dict[str, deque[Observation]] = field(default_factory=dict)


class ExperimentManager:
    def __init__(
        self, registry: ModelRegistry, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.runs: dict[str, Run] = {}
        self._snapshot: RegistrySnapshot | None = None

    def sync(self, snapshot: RegistrySnapshot) -> None:
        if snapshot is self._snapshot:
            return
        deployments = {d.id: d for m in snapshot.models.values() for d in m.deployments}
        for name in set(self.runs) - snapshot.experiments.keys():
            del self.runs[name]
            metrics.EXPERIMENT_PAUSED.remove(name)
        for name, config in snapshot.experiments.items():
            # Runtime enable/weight changes must not reset a latched guardrail.
            targets = [deployments[v.deployment_id] for v in config.variants]
            signature = config.model_dump_json() + "".join(
                d.model_dump_json(exclude={"enabled", "weight"}) for d in targets
            )
            generation = hashlib.sha256(signature.encode()).hexdigest()
            if name not in self.runs or self.runs[name].generation != generation:
                cap = config.guardrail.max_samples if config.guardrail else 1000
                self.runs[name] = Run(
                    config,
                    generation,
                    targets[0].logical_model,
                    samples={v.name: deque(maxlen=cap) for v in config.variants},
                )
                metrics.EXPERIMENT_PAUSED.labels(name).set(0)
                for variant in config.variants:
                    for outcome in ("success", "error", "fallback", "cancelled", "warmup"):
                        metrics.EXPERIMENT_RESULTS.labels(name, variant.name, outcome).inc(0)
        self._snapshot = snapshot

    def assign(
        self, model: str, ctx: RequestContext, snapshot: RegistrySnapshot
    ) -> Assignment | None:
        self.sync(snapshot)
        for name, run in self.runs.items():
            config = run.config
            if not config.enabled or run.model != model:
                continue
            bucket = {
                "session_id": ctx.session_id,
                "user_id": ctx.user_bucket,
                "request_id": ctx.request_id,
            }[config.bucket_key] or ctx.request_id
            variant = (
                next(v for v in config.variants if v.name == config.control)
                if run.paused
                else choose(name, config, bucket)
            )
            warmup = run.assigned[variant.name] < config.warmup_requests
            run.assigned[variant.name] += 1
            result = Assignment(
                name, variant.name, variant.deployment_id, run.generation, warmup, run.paused
            )
            log_event(
                log,
                "experiment_assigned",
                experiment=name,
                variant=variant.name,
                warmup=warmup,
                paused=run.paused,
            )
            return result
        return None

    def record(
        self,
        ctx: RequestContext,
        status: str,
        timings: ChatTimings | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        assignment = ctx.assignment
        if assignment is None or ctx.experiment_recorded:
            return
        ctx.experiment_recorded = True
        outcome = status
        if status != "cancelled":
            if assignment.warmup:
                outcome = "warmup"
            elif ctx.fallback_from or (
                ctx.deployment_id is not None and ctx.deployment_id != assignment.deployment_id
            ):
                outcome = "fallback"
        metrics.EXPERIMENT_RESULTS.labels(assignment.experiment, assignment.variant, outcome).inc()
        clean = outcome == "success"
        observation = Observation(
            self.clock(),
            outcome,
            timings.ttft_sec if clean and timings else None,
            timings.total_sec if clean and timings else None,
            timings.output_tps(output_tokens) if clean and timings else None,
            input_tokens if clean else None,
            output_tokens if clean else None,
            ctx.request_type
            if ctx.request_type in {"simple_qa", "report_analysis", "news_summary"}
            else "other",
        )
        if clean:
            for metric, value in (
                (metrics.EXPERIMENT_TTFT, observation.ttft),
                (metrics.EXPERIMENT_LATENCY, observation.latency),
                (metrics.EXPERIMENT_TPS, observation.tps),
            ):
                if value is not None:
                    metric.labels(assignment.experiment, assignment.variant).observe(value)
            for direction, count in (("input", input_tokens), ("output", output_tokens)):
                if count is not None:
                    metrics.EXPERIMENT_TOKENS.labels(
                        assignment.experiment, assignment.variant, direction
                    ).inc(count)
        self.sync(self.registry.snapshot)
        run = self.runs.get(assignment.experiment)
        if run is None or run.generation != assignment.generation:
            return  # Late completions cannot stop a replacement experiment.
        samples = run.samples[assignment.variant]
        samples.append(observation)
        self._prune(run)
        report = summarize(
            list(samples), run.config.guardrail.min_requests if run.config.guardrail else 20
        )
        reason = violation(report, run.config.guardrail)
        if reason and not run.paused:
            run.paused, run.reason = True, reason
            metrics.EXPERIMENT_PAUSED.labels(assignment.experiment).set(1)
            metrics.EXPERIMENT_GUARDRAILS.labels(
                assignment.experiment, assignment.variant, reason
            ).inc()
            log_event(
                log,
                "experiment_guardrail_triggered",
                level=logging.ERROR,
                experiment=assignment.experiment,
                variant=assignment.variant,
                reason=reason,
            )

    def _prune(self, run: Run) -> None:
        window = run.config.guardrail.window_sec if run.config.guardrail else 300
        cutoff = self.clock() - window
        for samples in run.samples.values():
            while samples and samples[0].at <= cutoff:
                samples.popleft()

    def admits(self, ctx: RequestContext, deployment_id: str) -> bool:
        self.sync(self.registry.snapshot)
        if ctx.assignment is None:
            return True
        run = self.runs.get(ctx.experiment)
        if run is None or not run.config.enabled:
            return False
        if run.paused:
            return any(
                v.name == run.config.control and v.deployment_id == deployment_id
                for v in run.config.variants
            )
        return any(v.deployment_id == deployment_id and v.weight > 0 for v in run.config.variants)

    def report(self) -> dict:
        self.sync(self.registry.snapshot)
        reports = {}
        for name, run in self.runs.items():
            self._prune(run)
            minimum = run.config.guardrail.min_requests if run.config.guardrail else 20
            reports[name] = {
                "enabled": run.config.enabled,
                "model": run.model,
                "paused": run.paused,
                "reason": run.reason,
                "generation": run.generation,
                "variants": {
                    name: segmented(list(samples), minimum) for name, samples in run.samples.items()
                },
            }
        return {"scope": "process", "experiments": reports}
