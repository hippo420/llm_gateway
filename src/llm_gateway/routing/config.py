"""Validated routing policy, published with the registry snapshot."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthThresholds(BaseModel):
    """Deployment-specific limits supplied from measured baselines, never guessed."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    max_error_rate: float | None = Field(default=None, ge=0, le=1)
    max_latency_p95_sec: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_limit(self) -> Self:
        if self.max_error_rate is None and self.max_latency_p95_sec is None:
            raise ValueError("at least one measured health threshold is required")
        return self


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    window_sec: float = Field(default=300, gt=0, le=86400)
    min_samples: int = Field(default=20, ge=1, le=10000, strict=True)
    max_samples: int = Field(default=1000, ge=1, le=10000, strict=True)
    thresholds: dict[str, HealthThresholds] = Field(min_length=1)

    @model_validator(mode="after")
    def check_capacity(self) -> Self:
        if self.min_samples > self.max_samples:
            raise ValueError("min_samples must not exceed max_samples")
        return self


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Literal["static", "weighted", "health_aware"] = "static"
    health_aware: HealthConfig | None = None
    bucket_header: str = Field(
        default="X-Session-Id",
        min_length=1,
        max_length=128,
        pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$",
    )

    @model_validator(mode="after")
    def require_health_config(self) -> Self:
        if self.strategy == "health_aware" and self.health_aware is None:
            raise ValueError("health_aware requires explicit deployment thresholds")
        return self
