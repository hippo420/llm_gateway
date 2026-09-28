from ..core.context import RequestContext
from ..registry.models import ModelDeployment
from ..resilience.config import ResilienceConfig, RetryConfig
from ..routing.decision import RoutingDecision
from ..schemas.chat import ChatCompletionRequest
from ..service.chat_service import ChatResult, ChatService


async def complete(
    service: ChatService,
    deployment: ModelDeployment,
    request: ChatCompletionRequest,
    request_id: str,
) -> ChatResult:
    context = RequestContext(request_id=request_id, model=deployment.logical_model)
    context.resilience_config = ResilienceConfig(retry=RetryConfig(max_attempts=1))
    context.routing_decision = RoutingDecision(deployment, "evaluation", "static")
    return await service.complete(request, context, deployment)
