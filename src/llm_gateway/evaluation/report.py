from collections import defaultdict
from typing import Any, get_args

from ..experiment.report import percentile
from .config import Criteria
from .models import Run, ScoreName, Summary, SummaryRow


def build_summary(run: Run) -> Summary:
    groups = defaultdict(list)
    for sample in run.samples:
        groups[(sample.dataset, sample.deployment_id)].append(sample)
    datasets = {d.name: d for d in run.datasets}
    rows = []
    for (dataset, deployment), samples in sorted(groups.items()):
        good = [s for s in samples if s.status == "success"]
        values = {
            name: [s.scores[name] for s in good if name in s.scores] for name in get_args(ScoreName)
        }
        ttft = [s.performance.ttft_sec for s in good if s.performance.ttft_sec is not None]
        latency = [s.performance.total_sec for s in good if s.performance.total_sec is not None]
        tps = [s.performance.output_tps for s in good if s.performance.output_tps is not None]
        rows.append(
            SummaryRow(
                dataset=dataset,
                deployment_id=deployment,
                request_type=datasets[dataset].request_type,
                provenance=datasets[dataset].provenance,
                samples=len(samples),
                successes=len(good),
                error_rate=(len(samples) - len(good)) / len(samples),
                scores={name: sum(v) / len(v) if v else None for name, v in values.items()},
                score_samples={name: len(v) for name, v in values.items()},
                ttft_p95_sec=percentile(ttft, 0.95),
                latency_p95_sec=percentile(latency, 0.95),
                output_tps_mean=sum(tps) / len(tps) if tps else None,
            )
        )
    return Summary(
        run_id=run.run_id,
        created_at=run.created_at,
        scorer_metadata=run.scorer_metadata,
        datasets=run.datasets,
        deployments=run.deployments,
        calibration=run.calibration,
        rows=rows,
        policy_input=policy_input(run, rows),
    )


def policy_input(run: Run, rows: list[SummaryRow]) -> dict:
    criteria = Criteria.model_validate(run.criteria)
    matrix: list[dict[str, Any]] = []
    groups = defaultdict(list)
    for row in rows:
        groups[(row.request_type, row.deployment_id)].append(row)
    for (request_type, deployment), group in sorted(groups.items()):
        total = sum(r.samples for r in group)
        scored = sum(r.score_samples["overall"] for r in group)
        overall = (
            sum((r.scores["overall"] or 0) * r.score_samples["overall"] for r in group) / scored
            if scored
            else None
        )
        error_rate = sum(r.error_rate * r.samples for r in group) / total
        reasons = []
        if any(r.provenance != "service" for r in group):
            reasons.append("synthetic_dataset")
        if run.calibration.status != "validated":
            reasons.append("judge_not_human_validated")
        if total < criteria.min_samples or scored < criteria.min_samples:
            reasons.append("insufficient_samples")
        if scored != sum(r.successes for r in group):
            reasons.append("incomplete_judge_coverage")
        if overall is None or overall < criteria.min_quality:
            reasons.append("quality_below_threshold")
        if error_rate > criteria.max_error_rate:
            reasons.append("error_rate_above_threshold")
        good = [
            s
            for s in run.samples
            if s.request_type == request_type
            and s.deployment_id == deployment
            and s.status == "success"
        ]
        ttft = [s.performance.ttft_sec for s in good if s.performance.ttft_sec is not None]
        latency = [s.performance.total_sec for s in good if s.performance.total_sec is not None]
        tps = [s.performance.output_tps for s in good if s.performance.output_tps is not None]
        matrix.append(
            {
                "request_type": request_type,
                "deployment_id": deployment,
                "quality": overall,
                "error_rate": error_rate,
                "samples": total,
                "performance": {
                    "ttft_p95_sec": percentile(ttft, 0.95),
                    "latency_p95_sec": percentile(latency, 0.95),
                    "output_tps_mean": sum(tps) / len(tps) if tps else None,
                },
                "eligible": not reasons,
                "reasons": reasons,
            }
        )
    recommendations = []
    for kind in sorted({r["request_type"] for r in matrix}):
        candidates = [r for r in matrix if r["request_type"] == kind and r["eligible"]]
        best = sorted(
            candidates,
            key=lambda r: (
                -r["quality"],
                r["performance"]["latency_p95_sec"]
                if r["performance"]["latency_p95_sec"] is not None
                else float("inf"),
                r["deployment_id"],
            ),
        )
        recommendations.append(
            {
                "request_type": kind,
                "deployment_id": best[0]["deployment_id"] if best else None,
            }
        )
    return {
        "schema_version": 1,
        "run_id": run.run_id,
        "mode": "advisory",
        "deployment_fingerprints": {
            key: value["config_sha256"] for key, value in run.deployments.items()
        },
        "automatic_routing_change": False,
        "criteria": run.criteria,
        "matrix": matrix,
        "recommendations": recommendations,
    }


def markdown(summary: Summary) -> str:
    def fmt(value):
        return "미측정" if value is None else f"{value:.3f}"

    lines = [
        "# 성능·품질 평가 결과",
        "",
        f"Run: `{summary.run_id}` / {summary.created_at}",
        f"사람 평가 상관 검증: **{summary.calibration.status}**",
        "",
        "| 요청 종류 | 데이터셋 | 배포 | 표본 | 오류율 | 규칙 | 유사도 |"
        " Judge overall | TTFT P95 | Latency P95 | TPS |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary.rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row.request_type,
                    row.dataset,
                    row.deployment_id,
                    str(row.samples),
                    fmt(row.error_rate),
                    fmt(row.scores["rule"]),
                    fmt(row.scores["similarity"]),
                    fmt(row.scores["overall"]),
                    fmt(row.ttft_p95_sec),
                    fmt(row.latency_p95_sec),
                    fmt(row.output_tps_mean),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "규칙 점수는 형식/포함 조건 충족률이며 사실성 점수가 아니다.",
        "임베딩 유사도는 의미 유사성이다. Context relevance는 검색 품질로 별도 해석한다.",
        "합성 데이터나 검증되지 않은 judge로는 라우팅 모델을 추천하지 않는다.",
        "동일 데이터셋 해시·생성 설정·judge 모델/프롬프트 버전에서 비교해야 한다.",
        "",
        "## 요청 종류별 적합도",
        "",
        "| 요청 종류 | 배포 | 후보 적격 | 보류 이유 |",
        "|---|---|---|---|",
    ]
    for row in summary.policy_input["matrix"]:
        lines.append(
            f"| {row['request_type']} | {row['deployment_id']} | {row['eligible']} | "
            f"{', '.join(row['reasons']) or '-'} |"
        )
    lines += [
        "",
        "## Judge / 사람 평가",
        "",
        f"표본 쌍: {summary.calibration.pairs}, 고유 질의: {summary.calibration.unique_cases}",
        f"Pearson: {fmt(summary.calibration.pearson)}, "
        f"Spearman: {fmt(summary.calibration.spearman)}, MAE: {fmt(summary.calibration.mae)}",
        "",
        "policy-input.json은 Phase 9용 후보 자료이며 운영 라우팅을 변경하지 않는다.",
        "",
    ]
    return "\n".join(lines)
