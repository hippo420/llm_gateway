"""헬스체크 엔드포인트.

두 개를 구분하는 이유:
  /healthz - 프로세스 생존만. 의존성 검사 없음. 항상 빠르게 200.
             (여기서 Ollama 를 체크하면 Ollama 가 죽을 때 컨테이너가 재시작된다)
  /readyz  - Registry 로드 + adapter 도달 가능 여부. 트래픽 투입 판단용.

인증을 걸지 않는다 (오케스트레이터가 호출한다).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ...adapters.factory import AdapterFactory
from ...registry.models import ModelRegistry
from ...schemas.common import HealthResponse, ReadyResponse
from ..dependencies import get_adapter_factory, get_registry

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """TODO: HealthResponse(status="ok") 를 그대로 반환. 아무것도 검사하지 않는다."""
    raise NotImplementedError


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(
    response: Response,
    registry: ModelRegistry = Depends(get_registry),
    adapters: AdapterFactory = Depends(get_adapter_factory),
) -> ReadyResponse:
    """TODO: 구현.

      1. registry 스냅샷 상태 (loaded, 모델 수, version)
      2. enabled deployment 마다 adapter.health() 를 **병렬로** 호출 (asyncio.gather)
         - 각 호출에 짧은 timeout (예: 2초). 순차 호출하면 readyz 가 느려진다
      3. 하나라도 unhealthy 면 response.status_code = 503, status = "degraded"

    주의: Ollama 는 모델이 언로드된 상태에서도 /api/tags 에 응답한다.
          즉 여기서 healthy 여도 첫 요청은 느릴 수 있다 (cold start).
          "모델이 GPU 에 올라와 있는가"는 별개 문제이므로 필요하면 warm-up 을 따로 둔다.
    """
    raise NotImplementedError
