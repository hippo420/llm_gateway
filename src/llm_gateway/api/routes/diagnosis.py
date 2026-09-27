"""Read-only operational diagnosis API; follows the gateway's bearer-key policy."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...diagnosis.engine import DiagnosisEngine
from ...diagnosis.report import Diagnosis
from ..dependencies import verify_api_key

router = APIRouter(prefix="/admin/diagnosis", dependencies=[Depends(verify_api_key)])


@router.get("")
async def current_diagnosis(request: Request) -> list[Diagnosis]:
    engine: DiagnosisEngine | None = request.app.state.diagnosis_engine
    return engine.current() if engine else []


@router.get("/status")
async def diagnosis_status(request: Request) -> dict:
    engine: DiagnosisEngine | None = request.app.state.diagnosis_engine
    return {
        "enabled": engine is not None,
        "last_evaluated_at": engine.last_evaluated_at if engine else None,
        "query_errors": engine.query_errors if engine else {},
        "rules": engine.rule_status if engine else {},
    }
