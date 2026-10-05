"""OpenAI-compatible embeddings request and response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input: list[str] = Field(min_length=1)
    encoding_format: Literal["float"] = "float"
    dimensions: int | None = Field(default=None, gt=0)

    @field_validator("input", mode="before")
    @classmethod
    def _normalize_input(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value

    @field_validator("input")
    @classmethod
    def _validate_input(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("input items must not be empty")
        return value


class EmbeddingUsage(BaseModel):
    prompt_tokens: int | None = None
    total_tokens: int | None = None


class EmbeddingItem(BaseModel):
    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[EmbeddingItem]
    model: str
    usage: EmbeddingUsage | None = None