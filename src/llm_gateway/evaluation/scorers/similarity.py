import math

import httpx

from ..config import SimilarityConfig


class SimilarityScorer:
    def __init__(self, config: SimilarityConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=config.timeout_sec, trust_env=False)
        self.owns_client = client is None

    async def score(self, answer: str, reference: str) -> float:
        response = await self.client.post(
            self.config.endpoint.rstrip("/") + "/api/embed",
            json={"model": self.config.model, "input": [answer, reference], "truncate": False},
            timeout=self.config.timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        vectors = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(vectors, list) or len(vectors) != 2:
            raise ValueError("expected two embedding vectors")
        a, b = vectors
        if (
            not isinstance(a, list)
            or not isinstance(b, list)
            or not a
            or len(a) != len(b)
            or len(a) > 32768
        ):
            raise ValueError("invalid embedding dimensions")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in a + b):
            raise ValueError("invalid embedding values")
        norm_a, norm_b = math.hypot(*a), math.hypot(*b)
        if not norm_a or not norm_b or not math.isfinite(norm_a + norm_b):
            raise ValueError("embedding norm must be finite and nonzero")
        cosine = math.fsum((x / norm_a) * (y / norm_b) for x, y in zip(a, b, strict=True))
        return (min(1.0, max(-1.0, cosine)) + 1) / 2

    async def aclose(self) -> None:
        if self.owns_client:
            await self.client.aclose()
