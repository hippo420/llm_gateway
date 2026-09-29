from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...core.errors import ConfigError
from ...policy.engine import PolicyEngine
from ...policy.models import Admission
from ..dependencies import verify_admin_key
from .config import actor

router = APIRouter(prefix="/admin", dependencies=[Depends(verify_admin_key)], tags=["policy"])


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


def engine(request: Request) -> PolicyEngine:
    value = getattr(request.app.state, "policy_engine", None)
    if value is None:
        raise ConfigError("policy controller is not enabled")
    return value


@router.get("/recommendations")
async def recommendations(controller: PolicyEngine = Depends(engine)) -> dict:
    state = await controller.state()
    records = sorted(
        (r for r in state.records.values() if r.status == "pending"),
        key=lambda r: r.created_at,
        reverse=True,
    )
    return {"recommendations": [controller.public(r) for r in records], "approval_required": True}


@router.get("/policy-history")
async def history(
    controller: PolicyEngine = Depends(engine),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> dict:
    state = await controller.state()
    records = sorted(state.records.values(), key=lambda r: r.created_at, reverse=True)
    return {
        "total": len(records),
        "records": [controller.public(r) for r in records[offset : offset + limit]],
    }


@router.post("/recommendations/{identifier}/approve")
async def approve(
    identifier: str,
    body: DecisionRequest,
    request: Request,
    controller: PolicyEngine = Depends(engine),
) -> dict:
    return controller.public(await controller.approve(identifier, actor(request), body.reason))


@router.post("/recommendations/{identifier}/reject")
async def reject(
    identifier: str,
    body: DecisionRequest,
    request: Request,
    controller: PolicyEngine = Depends(engine),
) -> dict:
    return controller.public(await controller.reject(identifier, actor(request), body.reason))


@router.post("/policy-history/{identifier}/rollback")
async def rollback(
    identifier: str,
    body: DecisionRequest,
    request: Request,
    controller: PolicyEngine = Depends(engine),
) -> dict:
    return controller.public(await controller.rollback(identifier, actor(request), body.reason))


@router.post("/policy-history/{identifier}/resolve")
async def resolve(
    identifier: str,
    body: DecisionRequest,
    request: Request,
    controller: PolicyEngine = Depends(engine),
) -> dict:
    return controller.public(await controller.resolve(identifier, actor(request), body.reason))


class SwitchRequest(DecisionRequest):
    enabled: bool = Field(strict=True)


class AdmissionRequest(DecisionRequest):
    evidence: Admission


@router.get("/automation")
async def automation_status(controller: PolicyEngine = Depends(engine)) -> dict:
    state = await controller.state()
    return {
        "configured": controller.config.auto_remediation.enabled,
        "enabled": controller.config.auto_remediation.enabled and state.automation_enabled,
        "grants": {name: grant.model_dump(mode="json") for name, grant in state.grants.items()},
    }


@router.put("/automation")
async def automation_switch(
    body: SwitchRequest,
    request: Request,
    controller: PolicyEngine = Depends(engine),
) -> dict:
    state = await controller.automation.set_enabled(body.enabled, actor(request), body.reason)
    return {"enabled": controller.config.auto_remediation.enabled and state.automation_enabled}


@router.post("/automation/{policy_id}/admit")
async def automation_admit(
    policy_id: str,
    body: AdmissionRequest,
    request: Request,
    controller: PolicyEngine = Depends(engine),
) -> dict:
    state = await controller.automation.admit(policy_id, body.evidence, actor(request), body.reason)
    return state.grants[policy_id].model_dump(mode="json")


@router.post("/automation/{policy_id}/demote")
async def automation_demote(
    policy_id: str,
    body: DecisionRequest,
    request: Request,
    controller: PolicyEngine = Depends(engine),
) -> dict:
    state = await controller.automation.demote(policy_id, actor(request), body.reason)
    return state.grants[policy_id].model_dump(mode="json")


@router.get("/automation-journal")
async def automation_journal(
    limit: int = Query(100, ge=1, le=1000),
    controller: PolicyEngine = Depends(engine),
) -> dict:
    return {"events": await controller.automation.journal(limit)}
