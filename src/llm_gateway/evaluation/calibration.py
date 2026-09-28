import hashlib
import json
import math
from pathlib import Path

from pydantic import Field

from .config import Criteria
from .models import Calibration, Name, Run, Score, StrictModel


class HumanRating(StrictModel):
    run_id: Name
    dataset: Name
    dataset_sha256: str
    case_id: Name
    deployment_id: str
    answer_sha256: str
    human_score: Score
    reviewer: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=2000)
    # Optional review material is local only; never included in the public report.
    answer: str | None = None
    question: str | None = None
    context: list[str] | None = None


def correlation(a: list[float], b: list[float]) -> float | None:
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    x, y = [v - mean_a for v in a], [v - mean_b for v in b]
    denominator = math.sqrt(sum(v * v for v in x) * sum(v * v for v in y))
    if not denominator:
        return None
    return max(-1.0, min(1.0, sum(i * j for i, j in zip(x, y, strict=True)) / denominator))


def ranks(values: list[float]) -> list[float]:
    result = [0.0] * len(values)
    order = sorted(range(len(values)), key=values.__getitem__)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[start]] == values[order[end]]:
            end += 1
        rank = (start + 1 + end) / 2
        for index in order[start:end]:
            result[index] = rank
        start = end
    return result


def calibrate(run: Run, path: Path) -> Calibration:
    if path.stat().st_size > 30_000_000:
        raise ValueError("ratings file exceeds 30 MB")
    data = path.read_bytes()
    ratings = [
        HumanRating.model_validate(json.loads(line))
        for line in data.decode("utf-8-sig").splitlines()
        if line.strip()
    ]
    samples = {(s.deployment_id, s.dataset, s.case_id): s for s in run.samples}
    datasets = {d.name: d.sha256 for d in run.datasets}
    seen = set()
    human, judge = [], []
    for rating in ratings:
        key = (rating.deployment_id, rating.dataset, rating.case_id)
        sample = samples.get(key)
        if key in seen:
            raise ValueError("duplicate human rating")
        if (
            rating.run_id != run.run_id
            or sample is None
            or rating.dataset_sha256 != datasets.get(rating.dataset)
            or rating.answer_sha256 != sample.answer_sha256
            or "overall" not in sample.scores
            or not rating.reviewer.strip()
        ):
            raise ValueError("human rating does not match a scored answer in this run")
        seen.add(key)
        human.append(rating.human_score)
        judge.append(sample.scores["overall"])
    unique = len({(dataset, case_id) for _, dataset, case_id in seen})
    result = Calibration(
        pairs=len(human), unique_cases=unique, ratings_sha256=hashlib.sha256(data).hexdigest()
    )
    criteria = Criteria.model_validate(run.criteria)
    if len(human) < criteria.min_human_cases or unique < criteria.min_human_cases:
        result.status = "insufficient_samples"
        return result
    result.pearson = correlation(human, judge)
    result.spearman = correlation(ranks(human), ranks(judge))
    result.mae = sum(abs(h - j) for h, j in zip(human, judge, strict=True)) / len(human)
    if result.pearson is None or result.spearman is None:
        result.status = "constant_scores"
    elif result.spearman < criteria.min_spearman or result.mae > criteria.max_mae:
        result.status = "below_threshold"
    else:
        result.status = "validated"
    return result
