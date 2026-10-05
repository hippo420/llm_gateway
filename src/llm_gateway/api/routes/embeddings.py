"""OpenAI-compatible embeddings endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ...adapters.base import AdapterEmbeddingRequest
from ...adapters.factory import AdapterFactory
from ...core.context import RequestContext
from ...registry.models import ModelRegistry
from ...schemas.embeddings import (
    EmbeddingItem,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
)
from ...service.chat_service import ChatService
from ..dependencies import (
    get_adapter_factory,
    get_chat_service,
    get_registry,
    get_request_ctx,
    verify_api_key,
)

router = APIRouter(tags=["embeddings"], dependencies=[Depends(verify_api_key)])


@router.post("/embeddings", response_model=EmbeddingResponse, summary="OpenAI-compatible embeddings")
async def create_embeddings(
    request: EmbeddingRequest,
    http_request: Request,
    registry: ModelRegistry = Depends(get_registry),
    adapters: AdapterFactory = Depends(get_adapter_factory),
    service: ChatService = Depends(get_chat_service),
    ctx: RequestContext = Depends(get_request_ctx),
) -> JSONResponse:
    estimated_tokens = (sum(len(item) for item in request.input) + 3) // 4
    deployment = service.select_deployment(
        request.model,
        ctx,
        http_request.headers,
        estimated_input_tokens=estimated_tokens,
    )
    with adapters.lease(deployment) as adapter:
        result = await adapter.embed(
            AdapterEmbeddingRequest(
                model=deployment.upstream_model,
                input=request.input,
                dimensions=request.dimensions,
            )
        )

    body = EmbeddingResponse(
        data=[EmbeddingItem(index=index, embedding=vector) for index, vector in enumerate(result.embeddings)],
        model=request.model,
        usage=(
            EmbeddingUsage(
                prompt_tokens=result.prompt_tokens,
                total_tokens=result.total_tokens,
            )
            if result.prompt_tokens is not None or result.total_tokens is not None
            else None
        ),
    )
    headers = {"X-Gateway-Deployment": deployment.id}
    if ctx.fallback_from:
        headers["X-Gateway-Fallback"] = deployment.id
    return JSONResponse(content=body.model_dump(mode="json"), headers=headers)