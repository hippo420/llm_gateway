"""OpenAI-compatible Chat Completions 스키마.

이것이 Spring AI 와의 계약이다. 필드명을 임의로 바꾸면 Spring AI 가 파싱에 실패한다.
명세: docs/specs/api-spec.md
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .common import Usage

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions 요청.

    model 은 **논리 모델명**이다 (예: "qwen-7b").
    실제 upstream 모델명(qwen2.5:7b)은 Registry 가 해석한다.
    """

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    stop: list[str] | None = None
    seed: int | None = None

    # OpenAI 스펙에 있으나 Gateway 가 아직 지원하지 않는 필드.
    # 조용히 무시하지 말고 GW-4002 로 거절하거나 경고 로그를 남긴다.
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    response_format: dict[str, Any] | None = None
    n: int | None = None

    @field_validator("messages")
    @classmethod
    def _messages_not_empty(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        """비어 있으면 ValueError -> 전역 핸들러가 GW-4000 으로 변환한다."""
        if not v:
            raise ValueError("messages must not be empty")
        return v

    def unsupported_fields(self) -> list[str]:
        """설정된 미지원 필드 이름 목록. 비어 있으면 정상."""
        fields = [
            name
            for name in ("tools", "tool_choice", "response_format")
            if getattr(self, name) is not None
        ]
        # choices 를 하나만 만들기 때문에 n > 1 도 미지원이다.
        if self.n is not None and self.n > 1:
            fields.append("n")
        return fields


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    """Non-streaming 응답.

    Spring AI 는 id/object/created/model/choices/usage 를 모두 기대한다. 빠짐없이 채울 것.
    """

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    # 요청한 **논리 모델명**을 그대로 돌려준다.
    # 실제 처리한 deployment 는 X-Gateway-Deployment 헤더로 알린다.
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage | None = None


class ChatCompletionDelta(BaseModel):
    role: Role | None = None
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """SSE 로 흘려보내는 streaming chunk."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice] = Field(default_factory=list)
    # 마지막 chunk 에만 (upstream 이 제공할 때)
    usage: Usage | None = None
