from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
RequestType = Literal["simple_qa", "report_analysis", "news_summary"]
ScoreName = Literal[
    "rule",
    "similarity",
    "faithfulness",
    "answer_relevance",
    "context_relevance",
    "citation_accuracy",
    "hallucination",
    "overall",
]
Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("created_at", check_fields=False)
    @classmethod
    def aware_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return parsed.astimezone(UTC).isoformat()


class Case(StrictModel):
    id: Name
    request_type: RequestType
    question: str = Field(min_length=1, max_length=20000)
    context: list[Annotated[str, Field(min_length=1, max_length=50000)]] = Field(
        min_length=1, max_length=30
    )
    reference_answer: str = Field(min_length=1, max_length=50000)
    must_include: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list, max_length=100
    )
    must_not_include: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list, max_length=100
    )
    expected_numbers: list[Annotated[str, Field(pattern=r"^-?\d+(?:\.\d+)?$")]] = Field(
        default_factory=list, max_length=100
    )
    min_chars: int = Field(default=1, ge=0, le=100000, strict=True)
    max_chars: int | None = Field(default=None, ge=1, le=100000, strict=True)
    expected_format: Literal["text", "json_object"] = "text"
    require_citations: bool = False

    @model_validator(mode="after")
    def valid_constraints(self) -> Case:
        if not self.question.strip() or not self.reference_answer.strip():
            raise ValueError("question/reference must not be blank")
        if self.max_chars is not None and self.max_chars < self.min_chars:
            raise ValueError("max_chars must be >= min_chars")
        if set(self.must_include) & set(self.must_not_include):
            raise ValueError("conflicting required and forbidden terms")
        return self


class DatasetInfo(StrictModel):
    name: Name
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: Literal["synthetic", "service"]
    request_type: RequestType
    cases: int = Field(ge=1, le=1000)


class Performance(StrictModel):
    total_sec: float | None = Field(default=None, ge=0)
    ttft_sec: float | None = Field(default=None, ge=0)
    output_tps: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class Sample(StrictModel):
    dataset: Name
    case_id: Name
    request_type: RequestType
    deployment_id: str = Field(min_length=1, max_length=200)
    status: Literal["success", "error"]
    error_code: str | None = None
    answer_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    answer: str | None = None
    finish_reason: str | None = None
    performance: Performance = Field(default_factory=Performance)
    scores: dict[ScoreName, Score] = Field(default_factory=dict)
    rule_checks: dict[str, bool] = Field(default_factory=dict)
    scorer_errors: dict[Literal["similarity", "judge"], str] = Field(default_factory=dict)


class Calibration(StrictModel):
    status: Literal[
        "not_run", "insufficient_samples", "constant_scores", "below_threshold", "validated"
    ] = "not_run"
    pairs: int = 0
    unique_cases: int = 0
    pearson: float | None = Field(default=None, ge=-1, le=1)
    spearman: float | None = Field(default=None, ge=-1, le=1)
    mae: Score | None = None
    ratings_sha256: str | None = None


class Run(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Name
    created_at: str
    datasets: list[DatasetInfo] = Field(min_length=1, max_length=20)
    deployments: dict[str, dict[str, str]]
    scorer_metadata: dict
    criteria: dict
    samples: list[Sample] = Field(max_length=10000)
    calibration: Calibration = Field(default_factory=Calibration)


class SummaryRow(StrictModel):
    dataset: Name
    request_type: RequestType
    deployment_id: str = Field(min_length=1, max_length=200)
    provenance: Literal["synthetic", "service"]
    samples: int = Field(ge=0)
    successes: int = Field(ge=0)
    error_rate: Score
    scores: dict[ScoreName, Score | None]
    score_samples: dict[ScoreName, int]
    ttft_p95_sec: float | None = Field(default=None, ge=0)
    latency_p95_sec: float | None = Field(default=None, ge=0)
    output_tps_mean: float | None = Field(default=None, ge=0)


class Summary(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Name
    created_at: str
    scorer_metadata: dict
    datasets: list[DatasetInfo]
    deployments: dict[str, dict[str, str]]
    calibration: Calibration
    rows: list[SummaryRow] = Field(max_length=200)
    policy_input: dict
