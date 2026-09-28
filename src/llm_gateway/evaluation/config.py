from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import Field, model_validator

from ..registry.loader import UniqueKeyLoader
from .models import Name, Score, StrictModel


class DatasetSpec(StrictModel):
    name: Name
    path: Path
    # Synthetic is explicit by default; never label generated examples as service traffic.
    provenance: Literal["synthetic", "service"] = "synthetic"


class SimilarityConfig(StrictModel):
    enabled: bool = False
    endpoint: str = "http://[::1]:11434"
    model: str = Field(default="bge-m3", min_length=1)
    timeout_sec: float = Field(default=30, gt=0, le=600)

    @model_validator(mode="after")
    def endpoint_url(self) -> Self:
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("embedding endpoint must be an HTTP URL without credentials/query")
        return self


class JudgeConfig(StrictModel):
    deployment_id: str | None = None
    prompt_version: str = Field(default="judge-v1", pattern=r"^judge-v1$")
    max_tokens: int = Field(default=1024, ge=128, le=8192)


class Criteria(StrictModel):
    min_samples: int = Field(default=20, ge=20, le=1000, strict=True)
    min_quality: Score = 0.8
    max_error_rate: Score = 0.05
    min_human_cases: int = Field(default=20, ge=20, le=10000, strict=True)
    min_spearman: float = Field(default=0.5, ge=0, le=1)
    max_mae: Score = 0.2


class EvaluationConfig(StrictModel):
    datasets: list[DatasetSpec] = Field(min_length=1, max_length=20)
    concurrency: int = Field(default=1, ge=1, le=32, strict=True)
    warmup: int = Field(default=0, ge=0, le=100, strict=True)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0, ge=0, le=2)
    seed: int = 0
    similarity: SimilarityConfig = Field(default_factory=SimilarityConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    criteria: Criteria = Field(default_factory=Criteria)

    @model_validator(mode="after")
    def unique_datasets(self) -> Self:
        if len({d.name for d in self.datasets}) != len(self.datasets):
            raise ValueError("duplicate dataset name")
        return self

    @classmethod
    def load(cls, path: Path) -> Self:
        config = cls.model_validate(
            yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        )
        # Dataset paths are relative to the evaluation config, not the caller's cwd.
        for dataset in config.datasets:
            dataset.path = (path.parent / dataset.path).resolve()
        return config
