from datetime import datetime
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

from .store import EvaluationStore


def quality_metrics(root: Path, deployment_ids: set[str]) -> bytes:
    summaries = EvaluationStore(root).summaries()
    if not summaries:
        return b""

    class Collector:
        def collect(self):
            scores = GaugeMetricFamily(
                "llm_gateway_quality_score",
                "Latest batch quality scores.",
                labels=["deployment_id", "dataset", "scorer"],
            )
            counts = GaugeMetricFamily(
                "llm_gateway_quality_samples",
                "Scored batch sample counts.",
                labels=["deployment_id", "dataset", "scorer"],
            )
            timestamps = GaugeMetricFamily(
                "llm_gateway_quality_last_run_timestamp_seconds",
                "Latest batch creation time.",
                labels=["deployment_id", "dataset"],
            )
            latency = GaugeMetricFamily(
                "llm_gateway_quality_latency_seconds",
                "Successful batch latency P95.",
                labels=["deployment_id", "dataset", "stage"],
            )
            tps = GaugeMetricFamily(
                "llm_gateway_quality_output_tokens_per_second",
                "Successful batch mean output TPS.",
                labels=["deployment_id", "dataset"],
            )
            validated = GaugeMetricFamily(
                "llm_gateway_quality_judge_validated",
                "Batch judge passed human calibration (0/1).",
                labels=["deployment_id", "dataset"],
            )
            synthetic = GaugeMetricFamily(
                "llm_gateway_quality_dataset_synthetic",
                "Dataset is synthetic (0/1).",
                labels=["deployment_id", "dataset"],
            )
            seen = set()
            for summary in summaries:
                for row in summary.rows:
                    key = (row.deployment_id, row.dataset)
                    if row.deployment_id not in deployment_ids or key in seen:
                        continue
                    seen.add(key)
                    timestamps.add_metric(
                        list(key), datetime.fromisoformat(summary.created_at).timestamp()
                    )
                    validated.add_metric(list(key), int(summary.calibration.status == "validated"))
                    synthetic.add_metric(list(key), int(row.provenance == "synthetic"))
                    for stage, value in (
                        ("ttft", row.ttft_p95_sec),
                        ("total", row.latency_p95_sec),
                    ):
                        if value is not None:
                            latency.add_metric([*key, stage], value)
                    if row.output_tps_mean is not None:
                        tps.add_metric(list(key), row.output_tps_mean)
                    for scorer, value in row.scores.items():
                        count = row.score_samples.get(scorer, 0)
                        if value is not None and count > 0:
                            scores.add_metric([*key, scorer], value)
                            counts.add_metric([*key, scorer], count)
            yield scores
            yield counts
            yield timestamps
            yield latency
            yield tps
            yield validated
            yield synthetic

    registry = CollectorRegistry()
    registry.register(Collector())
    return generate_latest(registry)
