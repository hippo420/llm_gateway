from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ..diagnosis.rules import Condition, Rule
from ..registry.loader import UniqueKeyLoader
from ..registry.overrides import DeploymentOverride, TimeoutPatch

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WeightAction(StrictModel):
    type: Literal["adjust_weight"]
    target: str = Field(min_length=1)
    delta: int = Field(ge=-100, le=100, strict=True)

    @model_validator(mode="after")
    def nonzero(self) -> Self:
        if self.delta == 0:
            raise ValueError("weight delta must be nonzero")
        return self


class DisableAction(StrictModel):
    type: Literal["disable_deployment"]
    target: str = Field(min_length=1)


class TimeoutAction(StrictModel):
    type: Literal["set_timeout"]
    target: str = Field(min_length=1)
    timeout: TimeoutPatch

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.timeout.model_dump(exclude_none=True):
            raise ValueError("timeout action must change at least one field")
        return self


Action = Annotated[WeightAction | DisableAction | TimeoutAction, Field(discriminator="type")]


class Guard(StrictModel):
    require_approval: Literal[True] = True
    max_change_per_hour: int = Field(default=2, ge=1, le=20, strict=True)
    cooldown_sec: int = Field(default=900, ge=60, le=86400, strict=True)
    min_weight: int = Field(default=10, ge=1, le=100, strict=True)
    max_weight: int = Field(default=100, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.min_weight > self.max_weight:
            raise ValueError("min_weight must not exceed max_weight")
        return self


class ValidationCheck(StrictModel):
    deployment_id: str = Field(min_length=1)
    conditions: list[Condition] = Field(min_length=1, max_length=10)
    min_samples: int = Field(default=20, ge=1, le=100000, strict=True)

    def rule(self) -> Rule:
        # Exactly the Phase 3 three-valued evaluator, including missing-signal semantics.
        return Rule("policy_validation", "info", tuple(self.conditions), "", (), 1)


class Validation(StrictModel):
    observe_sec: int = Field(default=600, ge=300, le=86400, strict=True)
    # Diagnosis default_queries uses a five-minute range. Never validate a pre-change window.
    window_sec: Literal[300] = 300
    rollback_on_failure: Literal[True] = True
    checks: list[ValidationCheck] = Field(min_length=1, max_length=10)


class QualityGate(StrictModel):
    run_id: Identifier
    request_type: Literal["simple_qa", "report_analysis", "news_summary"]
    max_age_sec: int = Field(default=86400, ge=60, le=2592000, strict=True)


class Policy(StrictModel):
    id: Identifier
    enabled: bool = True
    # Refers to a confirmed Phase 3 diagnosis, rather than copying trigger thresholds.
    trigger_rule_id: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    actions: list[Action] = Field(min_length=1, max_length=10)
    guard: Guard
    validation: Validation
    expected_effect: str = Field(min_length=1, max_length=1000)
    recommendation_ttl_sec: int = Field(default=900, ge=60, le=86400, strict=True)
    override_ttl_sec: int = Field(default=3600, ge=600, le=86400, strict=True)
    quality: QualityGate | None = None

    @model_validator(mode="after")
    def reversible(self) -> Self:
        if len({a.target for a in self.actions}) != len(self.actions):
            raise ValueError("only one action per deployment is allowed")
        if self.override_ttl_sec <= self.validation.observe_sec + 60:
            raise ValueError("override TTL must exceed observation by more than 60 seconds")
        if any(isinstance(a, WeightAction) for a in self.actions):
            if not all(isinstance(a, WeightAction) for a in self.actions):
                raise ValueError("weight transfers must not mix action types")
            if sum(a.delta for a in self.actions if isinstance(a, WeightAction)) != 0:
                raise ValueError("weight transfers must conserve total weight")
        return self


class PolicyConfig(StrictModel):
    interval_sec: int = Field(default=15, ge=5, le=300, strict=True)
    max_active: int = Field(default=1, ge=1, le=10, strict=True)
    max_changes_per_hour: int = Field(default=4, ge=1, le=50, strict=True)
    max_pending: int = Field(default=20, ge=1, le=100, strict=True)
    policies: list[Policy] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({p.id for p in self.policies}) != len(self.policies):
            raise ValueError("duplicate policy ID")
        return self

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate(
            yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        )


Status = Literal[
    "pending",
    "rejected",
    "expired",
    "observing",
    "validated",
    "rolled_back",
    "rollback_conflict",
    "resolved",
]


class Recommendation(StrictModel):
    id: Identifier
    policy: Policy
    policy_config_hash: str
    trigger: str
    trigger_evidence: dict
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: Status = "pending"
    before_config: dict
    after_config: dict
    base_hash: str
    config_hash: str
    before_overrides: dict[str, DeploymentOverride | None]
    before_expirations: dict[str, AwareDatetime | None]
    after_overrides: dict[str, DeploymentOverride]
    applied_expirations: dict[str, AwareDatetime] = Field(default_factory=dict)
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None
    rejected_by: str | None = None
    rejected_reason: str | None = None
    applied_at: AwareDatetime | None = None
    validation_result: dict = Field(default_factory=dict)
    rolled_back: bool = False
    rolled_back_at: AwareDatetime | None = None
    events: list[dict] = Field(default_factory=list)

    def event(self, name: str, at: datetime, actor: str, reason: str) -> None:
        self.events.append({"event": name, "at": at.isoformat(), "actor": actor, "reason": reason})


class PolicyState(StrictModel):
    schema_version: Literal[1] = 1
    # No TTL or automatic deletion. Archive explicitly before this bound is reached.
    records: dict[str, Recommendation] = Field(default_factory=dict, max_length=10000)
