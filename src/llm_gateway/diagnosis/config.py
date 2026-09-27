"""Separate process-start diagnosis configuration; no invented production baselines."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .baseline import FixedBaseline
from .rules import Thresholds
from .signals import SIGNAL_NAMES, default_queries


class DiagnosisTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    baseline: FixedBaseline
    thresholds: Thresholds
    queries: dict[str, str] = Field(default_factory=dict)

    @field_validator("queries")
    @classmethod
    def validate_queries(cls, queries: dict[str, str]) -> dict[str, str]:
        if queries.keys() - SIGNAL_NAMES or any(not query.strip() for query in queries.values()):
            raise ValueError("query overrides require known signals and nonempty PromQL")
        return queries

    def signal_queries(self) -> dict[str, str]:
        return default_queries(self.model, self.deployment_id) | self.queries


class DiagnosisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    prometheus_url: str = "http://localhost:9090"
    query_timeout_sec: float = Field(default=10, gt=0)
    interval_sec: int = Field(default=60, ge=15)
    consecutive_matches: int = Field(default=3, ge=1)
    cooldown_sec: float = Field(default=300, ge=0)
    targets: list[DiagnosisTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_targets(self) -> DiagnosisConfig:
        ids = [target.deployment_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("diagnosis deployment_id must be unique")
        if self.interval_sec % 15:
            raise ValueError("interval_sec must align with the 15-second scrape interval")
        return self

    @classmethod
    def load(cls, path: Path) -> DiagnosisConfig:
        with path.open(encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))
