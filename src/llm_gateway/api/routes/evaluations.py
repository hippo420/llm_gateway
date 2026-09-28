import asyncio

from fastapi import APIRouter, Depends, Request

from ...core.errors import EvaluationNotFoundError
from ...evaluation.store import EvaluationStore
from ..dependencies import verify_admin_key

router = APIRouter(prefix="/admin/evaluations", dependencies=[Depends(verify_admin_key)])


def store(request: Request) -> EvaluationStore:
    return EvaluationStore(request.app.state.settings.evaluation_results_path)


@router.get("")
async def evaluations(request: Request) -> dict:
    summaries = await asyncio.to_thread(store(request).summaries)
    return {"runs": [summary.model_dump(mode="json") for summary in summaries]}


@router.get("/{run_id}")
async def evaluation(run_id: str, request: Request) -> dict:
    try:
        summary = await asyncio.to_thread(store(request).load_summary, run_id)
    except (OSError, ValueError) as exc:
        raise EvaluationNotFoundError("evaluation run not found") from exc
    return summary.model_dump(mode="json")
