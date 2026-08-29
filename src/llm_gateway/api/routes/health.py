"""헬스체크 엔드포인트.

두 개를 구분하는 이유:
  /healthz - 프로세스 생존만. 의존성 검사 없음. 항상 빠르게 200.
             (여기서 Ollama 를 체크하면 Ollama 가 죽을 때 컨테이너가 재시작된다)
  /readyz  - Registry 로드 + adapter 도달 가능 여부. 트래픽 투입 판단용.

인증을 걸지 않는다 (오케스트레이터가 호출한다).
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Response

from ...adapters.factory import AdapterFactory
from ...registry.models import ModelDeployment, ModelRegistry
from ...schemas.common import (
    AdapterHealth,
    HealthResponse,
    ReadyResponse,
    RegistryHealth,
)
from ..dependencies import get_adapter_factory, get_registry

router = APIRouter(tags=["health"])

# adapter 하나가 느려도 readyz 전체가 늘어지지 않게 끊는다.
HEALTH_TIMEOUT_SEC = 2.0


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """프로세스 생존만 알린다. 아무것도 검사하지 않는다."""
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(
    response: Response,
    registry: ModelRegistry = Depends(get_registry),
    adapters: AdapterFactory = Depends(get_adapter_factory),
) -> ReadyResponse:
    """트래픽을 받을 준비가 됐는지 알린다.

    주의: Ollama 는 모델이 언로드된 상태에서도 /api/tags 에 응답한다.
          즉 여기서 healthy 여도 첫 요청은 느릴 수 있다 (cold start).
          "모델이 GPU 에 올라와 있는가"는 별개 문제다.
    """
    snapshot = registry.snapshot
    deployments = registry.all_deployments()

    # 순차 호출하면 deployment 수만큼 readyz 가 느려진다.
    results = await asyncio.gather(
        *(_check(adapters, dep) for dep in deployments),
        return_exceptions=False,
    )

    healthy = all(r.healthy for r in results)
    if not healthy:
        response.status_code = 503

    return ReadyResponse(
        status="ready" if healthy else "degraded",
        registry=RegistryHealth(
            loaded=True,
            models=len(snapshot.models),
            version=snapshot.loaded_at,
        ),
        adapters=list(results),
    )


async def _check(adapters: AdapterFactory, deployment: ModelDeployment) -> AdapterHealth:
    """adapter 하나의 도달 가능 여부. 예외는 여기서 흡수한다 (readyz 는 죽지 않는다)."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SEC):
            ok = await adapters.get(deployment).health()
    except TimeoutError:
        return AdapterHealth(
            deployment_id=deployment.id,
            healthy=False,
            error=f"health check timed out after {HEALTH_TIMEOUT_SEC}s",
        )
    except Exception as exc:  # noqa: BLE001 - readyz 는 어떤 경우에도 응답해야 한다
        return AdapterHealth(
            deployment_id=deployment.id,
            healthy=False,
            error=exc.__class__.__name__,
        )

    return AdapterHealth(
        deployment_id=deployment.id,
        healthy=ok,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        error=None if ok else "endpoint returned an error status",
    )
