"""모델 목록 엔드포인트."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...registry.models import ModelRegistry
from ...schemas.common import ModelCard, ModelDeploymentInfo, ModelList
from ..dependencies import get_registry, verify_api_key

router = APIRouter(tags=["models"], dependencies=[Depends(verify_api_key)])


@router.get("/models", response_model=ModelList, summary="논리 모델 목록")
async def list_models(registry: ModelRegistry = Depends(get_registry)) -> ModelList:
    """논리 모델과 그 후보 deployment 를 보여준다.

    deployments 는 OpenAI 표준 밖 확장 필드다 (Spring AI 는 무시한다).
    endpoint 주소나 api_key_env 는 노출하지 않는다.
    모델 목록은 인증만 통과하면 누구나 볼 수 있는 정보다.
    """
    return ModelList(
        data=[
            ModelCard(
                id=entry.name,
                description=entry.description,
                deployments=[
                    ModelDeploymentInfo(
                        id=dep.id,
                        adapter=dep.adapter,
                        enabled=dep.enabled,
                        weight=dep.weight,
                    )
                    # disabled deployment 도 보여준다. 왜 라우팅이 안 되는지 알아야 한다.
                    for dep in entry.deployments
                ],
            )
            for entry in registry.list_models()
        ]
    )
