"""Validated temporary overrides. Structural fields remain YAML-only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .models import GenerationOptions


class TimeoutPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    connect: float | None = Field(default=None, gt=0)
    read: float | None = Field(default=None, gt=0)
    total: float | None = Field(default=None, gt=0)


class DeploymentOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool | None = None
    weight: int | None = Field(default=None, ge=0, le=100, strict=True)
    timeout: TimeoutPatch | None = None
    options: GenerationOptions | None = None

    def patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude_unset=True)


class OverrideDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    updated_at: AwareDatetime | None = None
    updated_by: str | None = None
    reason: str | None = None
    deployments: dict[str, DeploymentOverride] = Field(default_factory=dict)
    expires_at: dict[str, AwareDatetime] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_expiration(self) -> OverrideDocument:
        if self.deployments.keys() != self.expires_at.keys():
            raise ValueError("every override requires its own expires_at timestamp")
        if self.deployments and not (self.updated_at and self.updated_by and self.reason):
            raise ValueError("override requires updated_at, updated_by and reason")
        return self

    def active(self, now: datetime | None = None) -> OverrideDocument:
        now = now or datetime.now(UTC)
        ids = {key for key, expiry in self.expires_at.items() if expiry > now}
        return self.model_copy(
            update={
                "deployments": {key: self.deployments[key] for key in ids},
                "expires_at": {key: self.expires_at[key] for key in ids},
            }
        )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Copy recursively; lists replace; None means unspecified."""
    from copy import deepcopy

    result = deepcopy(base)
    for key, value in override.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
