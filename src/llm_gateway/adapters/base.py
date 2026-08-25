"""LLMAdapter - Serving Framework 와의 유일한 경계.

이 경계가 새면 Phase 5 이후가 전부 무너진다. 규칙:

  1. Adapter 만이 upstream 프로토콜을 안다.
  2. Adapter 는 Gateway 의 OpenAI 스키마를 모른다 (아래 중립 DTO 만 안다).
  3. Adapter 는 재시도하지 않는다 (retry/fallback 은 Phase 6 의 상위 책임).
  4. Adapter 는 타이밍/토큰 원자료를 반드시 채운다 (Phase 2 의 입력).
  5. Adapter 는 자기 예외를 GatewayError 로 변환해서 올린다.

명세: docs/specs/adapter-interface.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..registry.models import ModelDeployment


@dataclass
class AdapterMessage:
    role: str
    content: str


@dataclass
class AdapterChatRequest:
    """중립 요청 DTO. OpenAI 스키마를 여기까지 내리지 않는다."""

    model: str                       # upstream 모델명 (논리명 아님)
    messages: list[AdapterMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    seed: int | None = None
    # adapter 고유 옵션 (예: ollama keep_alive).
    # 여러 adapter 가 공통으로 쓰기 시작하면 정식 필드로 승격할 것.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterUsage:
    """토큰 사용량.

    upstream 이 값을 주지 않으면 None 이다. **0 을 넣지 않는다.**
    0 = "토큰이 없었다", None = "모른다". 지표에서 완전히 다르다.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    source: str = "upstream"          # "upstream" | "estimated"


@dataclass
class AdapterTimings:
    """upstream 이 제공하는 타이밍 원자료. 전부 초 단위. 모르면 None."""

    queue_sec: float | None = None
    prompt_eval_sec: float | None = None
    generation_sec: float | None = None
    # 모델 적재 시간. 이 값이 크면 "서빙이 느린 것"이 아니라 "cold start" 다 (진단 R6).
    load_sec: float | None = None


@dataclass
class AdapterChatResponse:
    content: str
    finish_reason: str                # stop | length | error | cancelled
    usage: AdapterUsage
    timings: AdapterTimings
    upstream_model: str
    # 디버깅용 원본. 민감정보가 있을 수 있으므로 로그에 그대로 남기지 않는다.
    raw: dict[str, Any] | None = None


@dataclass
class AdapterChatChunk:
    """streaming 한 조각.

    TTFT 판정 기준은 **delta 가 빈 문자열이 아닌 첫 chunk** 다.
    role 만 담긴 빈 chunk 를 첫 토큰으로 세면 TTFT 가 실제보다 짧게 나온다.
    """

    delta: str = ""
    finish_reason: str | None = None   # 마지막 chunk 에만
    usage: AdapterUsage | None = None  # 마지막 chunk 에만 (제공 시)
    timings: AdapterTimings | None = None


class LLMAdapter(ABC):
    """모든 Serving Framework 어댑터의 기반."""

    name: ClassVar[str] = ""

    def __init__(self, deployment: ModelDeployment) -> None:
        self.deployment = deployment

    @abstractmethod
    async def chat(self, request: AdapterChatRequest) -> AdapterChatResponse:
        """Non-streaming 호출."""

    @abstractmethod
    def stream_chat(self, request: AdapterChatRequest) -> AsyncIterator[AdapterChatChunk]:
        """Streaming 호출.

        구현은 **async generator** 로 한다 (`async def` + 본문에 `yield`).
        그래야 호출 즉시 iterator 가 반환된다.
        await 해야 iterator 가 나오는 코루틴으로 만들면 호출부가 전부 어긋난다.
        """

    @abstractmethod
    async def health(self) -> bool:
        """endpoint 도달 가능 여부. /readyz 에서 사용."""

    async def aclose(self) -> None:
        """HTTP client 등 자원 정리. 기본은 no-op."""
        return None
