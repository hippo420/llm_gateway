from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Identifier
    deployment_id: str = Field(min_length=1)
    weight: int = Field(ge=0, le=100, strict=True)


class Guardrail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    error_rate_max: float | None = Field(default=None, ge=0, le=1)
    ttft_p95_max_sec: float | None = Field(default=None, gt=0)
    min_requests: int = Field(default=20, ge=1, le=10000, strict=True)
    window_sec: float = Field(default=300, gt=0, le=86400)
    max_samples: int = Field(default=1000, ge=1, le=10000, strict=True)

    @model_validator(mode="after")
    def check_limits(self) -> Self:
        if self.error_rate_max is None and self.ttft_p95_max_sec is None:
            raise ValueError("at least one guardrail threshold is required")
        if self.min_requests > self.max_samples:
            raise ValueError("min_requests must not exceed max_samples")
        return self


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = Field(default=False, strict=True)
    description: str = ""
    bucket_key: Literal["session_id", "user_id", "request_id"] = "session_id"
    control: Identifier = "control"
    warmup_requests: int = Field(default=0, ge=0, le=10000, strict=True)
    variants: list[Variant] = Field(min_length=2, max_length=10)
    guardrail: Guardrail | None = None

    @model_validator(mode="after")
    def check_variants(self) -> Self:
        names = [v.name for v in self.variants]
        deployments = [v.deployment_id for v in self.variants]
        if len(set(names)) != len(names) or len(set(deployments)) != len(deployments):
            raise ValueError("variant names and deployments must be unique")
        if self.control not in names or not sum(v.weight for v in self.variants):
            raise ValueError("control and positive total weight are required")
        return self
