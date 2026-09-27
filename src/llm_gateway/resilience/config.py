from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RetryConfig(PolicyConfig):
    max_attempts: int = Field(default=2, ge=1, le=5, strict=True)
    backoff: Literal["exponential"] = "exponential"
    initial_delay_ms: int = Field(default=200, ge=0, strict=True)
    max_delay_ms: int = Field(default=2000, ge=0, le=60000, strict=True)
    jitter: bool = Field(default=True, strict=True)
    retry_on_stream_started: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def safe_retry(self) -> "RetryConfig":
        if self.retry_on_stream_started:
            raise ValueError("retry after output is forbidden")
        if self.initial_delay_ms > self.max_delay_ms:
            raise ValueError("initial delay must not exceed maximum delay")
        return self


class FallbackConfig(PolicyConfig):
    enabled: bool = Field(default=False, strict=True)
    # Includes the initial deployment, not just alternatives.
    max_chain: int = Field(default=2, ge=1, le=5, strict=True)


class BreakerConfig(PolicyConfig):
    enabled: bool = Field(default=False, strict=True)
    failure_threshold: int = Field(default=5, ge=1, strict=True)
    cooldown_sec: float = Field(default=30, gt=0)


class ResilienceConfig(PolicyConfig):
    retry: RetryConfig = Field(default_factory=RetryConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    circuit_breaker: BreakerConfig = Field(default_factory=BreakerConfig)
