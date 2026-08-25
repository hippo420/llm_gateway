"""모델 목록 엔드포인트."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...registry.models import ModelRegistry
from ...schemas.common import ModelList
from ..dependencies import get_registry, verify_api_key

router = APIRouter(tags=["models"], dependencies=[Depends(verify_api_key)])


@router.get("/models", response_model=ModelList, summary="논리 모델 목록")
async def list_models(registry: ModelRegistry = Depends(get_registry)) -> ModelList:
    """TODO: 구현.

      registry.list_models() -> ModelCard 변환.
      deployments 는 OpenAI 표준 밖 확장 필드다 (Spring AI 는 무시한다).

    주의: endpoint 주소나 api_key_env 를 여기에 노출하지 않는다.
          모델 목록은 인증만 통과하면 누구나 볼 수 있는 정보다.
    """
    raise NotImplementedError
