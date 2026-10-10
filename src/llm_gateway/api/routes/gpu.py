"""Authenticated GPU diagnostics for the Gateway host and offline lab results."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request

from ...observability.gpu import gpu_status, latest_benchmark
from ..dependencies import verify_admin_key

router = APIRouter(prefix="/admin/gpu", tags=["gpu"],
                   dependencies=[Depends(verify_admin_key)])


@router.get("")
async def status() -> dict:
    return await asyncio.to_thread(gpu_status)


@router.get("/benchmark/latest")
async def benchmark(request: Request) -> dict:
    return await asyncio.to_thread(
        latest_benchmark, request.app.state.settings.gpu_benchmark_results_path,
    )
