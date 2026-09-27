"""Authenticated, audited operational config controls."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, model_validator

from ...core.errors import ConfigError
from ...registry.manager import ConfigManager
from ...registry.overrides import DeploymentOverride
from ..dependencies import verify_admin_key

router = APIRouter(prefix="/admin", dependencies=[Depends(verify_admin_key)], tags=["config"])


class OverrideRequest(DeploymentOverride):
    reason: str = Field(min_length=1, max_length=500)
    ttl_sec: int = Field(default=3600, ge=1, le=86400, strict=True)

    @model_validator(mode="after")
    def require_change(self) -> OverrideRequest:
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        patch = self.override().patch()
        if not any(value != {} for value in patch.values()):
            raise ValueError("at least one override field is required")
        return self

    def override(self) -> DeploymentOverride:
        return DeploymentOverride.model_validate(
            self.model_dump(
                exclude={"reason", "ttl_sec"},
                exclude_none=True,
                exclude_unset=True,
            )
        )


def manager(request: Request) -> ConfigManager:
    value = getattr(request.app.state, "config_manager", None)
    if value is None:
        raise ConfigError("config manager is not initialized")
    return value


def actor(request: Request) -> str:
    # Identifies the authenticated shared credential without logging the credential itself.
    digest = hashlib.sha256(request.app.state.settings.api_key.encode()).hexdigest()[:12]
    return f"api-key:{digest}"


@router.get("/config")
async def effective_config(config: ConfigManager = Depends(manager)) -> dict:
    return config.effective()


@router.get("/config/sources")
async def config_sources(config: ConfigManager = Depends(manager)) -> dict:
    return config.sources()


@router.post("/config/reload")
async def reload_config(config: ConfigManager = Depends(manager)) -> dict:
    changed = await config.reload()
    return {"changed": changed, **config.effective()}


@router.put("/deployments/{deployment_id}")
async def put_override(
    deployment_id: str,
    body: OverrideRequest,
    request: Request,
    config: ConfigManager = Depends(manager),
) -> dict:
    await config.change_override(
        deployment_id,
        body.override(),
        actor=actor(request),
        reason=body.reason,
        ttl_sec=body.ttl_sec,
    )
    return config.effective()


@router.delete("/deployments/{deployment_id}/override")
async def delete_override(
    deployment_id: str,
    request: Request,
    reason: str = Query(min_length=1, max_length=500, pattern=r"\S"),
    config: ConfigManager = Depends(manager),
) -> dict:
    await config.change_override(deployment_id, None, actor=actor(request), reason=reason)
    return config.effective()
