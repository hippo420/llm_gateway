"""공통 응답 스키마."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Usage(BaseModel):
    """OpenAI 호환 토큰 사용량.

    Spring AI 가 이 필드를 읽으므로 이름을 바꾸지 말 것.
    upstream 이 토큰을 주지 않으면 0 이 아니라 None 이 들어간다.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ErrorDetail(BaseModel):
    message: str
    type: str
    code: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """모든 에러 응답의 형태. docs/specs/error-codes.md"""

    error: ErrorDetail


class ModelDeploymentInfo(BaseModel):
    """GET /v1/models 의 확장 필드. OpenAI 표준 밖이라 Spring AI 는 무시한다."""

    id: str
    adapter: str
    enabled: bool
    weight: int


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "llm-gateway"
    description: str | None = None
    deployments: list[ModelDeploymentInfo] = Field(default_factory=list)


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelCard] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"


class AdapterHealth(BaseModel):
    deployment_id: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


class RegistryHealth(BaseModel):
    loaded: bool
    models: int
    version: str | None = None


class ReadyResponse(BaseModel):
    status: str  # ready | degraded
    registry: RegistryHealth
    adapters: list[AdapterHealth] = Field(default_factory=list)
