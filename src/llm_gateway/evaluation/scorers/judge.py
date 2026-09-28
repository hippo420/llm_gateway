import hashlib
import json
import re
from importlib.resources import files

from ...registry.models import ModelDeployment
from ...schemas.chat import ChatCompletionRequest, ChatMessage
from ...service.chat_service import ChatService
from ..call import complete
from ..config import JudgeConfig
from ..models import Case, Score, StrictModel


class JudgeScores(StrictModel):
    faithfulness: Score
    answer_relevance: Score
    context_relevance: Score
    citation_accuracy: Score | None
    hallucination: Score

    def scores(self) -> dict:
        values = self.model_dump(exclude_none=True)
        dimensions = [self.faithfulness, self.answer_relevance, 1 - self.hallucination]
        if self.citation_accuracy is not None:
            dimensions.append(self.citation_accuracy)
        values["overall"] = sum(dimensions) / len(dimensions)
        return values


class JudgeScorer:
    def __init__(
        self, service: ChatService, deployment: ModelDeployment, config: JudgeConfig
    ) -> None:
        self.service, self.deployment, self.config = service, deployment, config
        self.prompt = (
            files("llm_gateway.evaluation")
            .joinpath(f"prompts/{config.prompt_version}.txt")
            .read_text(encoding="utf-8")
        )
        self.prompt_sha256 = hashlib.sha256(self.prompt.encode()).hexdigest()

    async def score(self, case: Case, answer: str) -> dict:
        request = ChatCompletionRequest(
            model=self.deployment.logical_model,
            messages=[
                ChatMessage(role="system", content=self.prompt),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "question": case.question,
                            "contexts": case.context,
                            "reference_answer": case.reference_answer,
                            "answer": answer,
                            "require_citations": case.require_citations,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            temperature=0,
            seed=0,
            max_tokens=self.config.max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "rag_quality", "schema": JudgeScores.model_json_schema()},
            },
        )
        result = await complete(self.service, self.deployment, request, f"judge-{case.id}")
        if result.response.choices[0].finish_reason != "stop":
            raise ValueError("judge output is incomplete")
        scores = JudgeScores.model_validate_json(result.response.choices[0].message.content)
        if (
            case.require_citations or re.search(r"\[\d+\]", answer)
        ) and scores.citation_accuracy is None:
            raise ValueError("required citation score is missing")
        return scores.scores()
