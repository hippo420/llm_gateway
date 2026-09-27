"""Declarative rules with three-valued AND/OR evaluation."""

from __future__ import annotations

import math
import operator
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .signals import SIGNAL_NAMES

Severity = Literal["info", "warning", "critical"]
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
VALID_SIGNALS = SIGNAL_NAMES | {
    f"{name}_{suffix}" for name in SIGNAL_NAMES for suffix in ("ratio", "delta")
}


@dataclass(frozen=True)
class Condition:
    signal: str
    op: Literal[">", ">=", "<", "<="]
    threshold: float

    def __post_init__(self) -> None:
        if self.signal not in VALID_SIGNALS or self.op not in OPS:
            raise ValueError("invalid condition signal or operator")
        if not math.isfinite(self.threshold):
            raise ValueError("condition threshold must be finite")

    def evaluate(self, values: dict[str, float | None]) -> bool | None:
        value = values.get(self.signal)
        if value is None or not math.isfinite(value):
            return None
        return OPS[self.op](value, self.threshold)


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    conditions: tuple[Condition, ...]
    hypothesis: str
    actions: tuple[str, ...]
    confidence: float
    any_conditions: tuple[Condition, ...] = ()

    def evaluate(self, values: dict[str, float | None]) -> bool | None:
        results = [condition.evaluate(values) for condition in self.conditions]
        if self.any_conditions:
            alternatives = [condition.evaluate(values) for condition in self.any_conditions]
            results.append(
                True if True in alternatives else None if None in alternatives else False
            )
        # Missing is distinct from normal. A known false AND operand still disproves the rule.
        return False if False in results else None if None in results else True


class Thresholds(BaseModel):
    """No operational defaults: calibrate these against the deployment's Phase 2 data."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    ttft_increase_ratio: float = Field(gt=1)
    ttft_extreme_ratio: float = Field(gt=1)
    tps_decrease_ratio: float = Field(gt=0, lt=1)
    queue_increase_delta: float = Field(ge=0)
    input_increase_ratio: float = Field(gt=1)
    gpu_increase_ratio: float = Field(gt=1)
    gpu_saturation_percent: float = Field(gt=0, le=100)
    gpu_low_percent: float = Field(ge=0, lt=100)
    memory_pressure_ratio: float = Field(gt=0, le=1)
    error_increase_delta: float = Field(ge=0, lt=1)
    cold_request_rate_max: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> Thresholds:
        if self.ttft_extreme_ratio <= self.ttft_increase_ratio:
            raise ValueError("extreme TTFT threshold must exceed the increase threshold")
        if self.gpu_low_percent >= self.gpu_saturation_percent:
            raise ValueError("low GPU threshold must be below the saturation threshold")
        return self


def build_rules(t: Thresholds) -> tuple[Rule, ...]:
    ttft = Condition("ttft_p95_ratio", ">", t.ttft_increase_ratio)
    queue_normal = Condition("queue_depth_delta", "<=", t.queue_increase_delta)
    error = Condition("error_rate_delta", ">", t.error_increase_delta)
    return (
        Rule(
            "TTFT_QUEUE_GPU_SATURATION",
            "warning",
            (
                ttft,
                Condition("queue_depth_delta", ">", t.queue_increase_delta),
                Condition("gpu_utilization", ">", t.gpu_saturation_percent),
            ),
            "Serving Concurrency 또는 GPU Scheduling 병목 가능성",
            (
                "Serving concurrency 상한 확인",
                "동시 요청 수 제한 검토",
                "vLLM continuous batching 비교 검토",
            ),
            0.7,
        ),
        Rule(
            "CONTEXT_LENGTH_PREFILL",
            "warning",
            (ttft, queue_normal, Condition("input_tokens_p95_ratio", ">", t.input_increase_ratio)),
            "Context 길이 증가에 따른 Prompt Processing(prefill) 병목 가능성",
            ("Spring RAG 컨텍스트 길이와 검색 문서 수 확인", "입력 토큰 P95와 baseline 비교"),
            0.75,
        ),
        Rule(
            "GENERATION_GPU_BOTTLENECK",
            "warning",
            (
                Condition("output_tps_ratio", "<", t.tps_decrease_ratio),
                Condition("gpu_utilization_ratio", ">", t.gpu_increase_ratio),
            ),
            "Generation(decode) 단계 GPU 병목 가능성",
            ("GPU 사용률 및 동시 생성 요청 확인", "출력 토큰 제한 및 batching 설정 검토"),
            0.7,
        ),
        Rule(
            "GPU_MEMORY_PRESSURE",
            "critical",
            (Condition("gpu_memory_used_ratio", ">", t.memory_pressure_ratio),),
            "KV cache/모델 메모리 부족 가능성. concurrency 또는 max_context 축소 필요",
            ("GPU 메모리와 KV cache 사용량 확인", "concurrency 또는 max_context 축소 검토"),
            0.8,
            any_conditions=(ttft, error),
        ),
        Rule(
            "UPSTREAM_ANOMALY",
            "warning",
            (error, queue_normal, Condition("gpu_utilization_ratio", "<=", t.gpu_increase_ratio)),
            "Serving 프로세스/네트워크 이상 가능성. 모델 언로드 또는 endpoint 장애 의심",
            ("Serving 프로세스 상태와 로그 확인", "endpoint 연결 및 모델 적재 상태 확인"),
            0.65,
        ),
        Rule(
            "MODEL_COLD_START",
            "info",
            (
                Condition("ttft_p95_ratio", ">", t.ttft_extreme_ratio),
                queue_normal,
                Condition("gpu_utilization", "<", t.gpu_low_percent),
                Condition("request_rate", ">", 0),
                Condition("request_rate", "<", t.cold_request_rate_max),
            ),
            "Cold start (모델 언로드 후 재적재) 가능성",
            ("Ollama keep_alive 설정 확인", "chat_completed 로그의 load_sec 확인"),
            0.5,
        ),
    )
