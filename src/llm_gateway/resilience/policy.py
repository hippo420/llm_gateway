"""One streaming executor for both API modes; retries never replay received output."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing

from ..adapters.base import AdapterChatChunk
from ..core.context import RequestContext
from ..core.errors import (
    AllFallbacksFailedError,
    GatewayError,
    NoAvailableDeploymentError,
    UpstreamProtocolError,
    UpstreamTotalTimeoutError,
)
from ..core.logging import log_event
from ..observability.metrics import FALLBACKS, RETRIES, TIMEOUTS
from ..registry.models import ModelDeployment, ModelRegistry
from ..routing.decision import RoutingDecision
from .breaker import CircuitBreakers
from .config import ResilienceConfig
from .fallback import chain, current_candidate
from .retry import backoff_seconds, breaker_failure, can_retry

log = logging.getLogger(__name__)
TIMEOUT_KINDS = {"GW-5003": "connect", "GW-5004": "read", "GW-5005": "total"}


class ResiliencePolicy:
    def __init__(
        self,
        registry: ModelRegistry,
        breakers: CircuitBreakers,
        *,
        observe: Callable[[ModelDeployment, bool, float], None] | None = None,
        available: Callable[[ModelDeployment], bool] | None = None,
    ) -> None:
        self.registry = registry
        self.breakers = breakers
        self.observe = observe
        self.available = available

    async def execute(
        self,
        decision: RoutingDecision,
        config: ResilienceConfig,
        ctx: RequestContext,
        call: Callable[[ModelDeployment], AsyncGenerator[AdapterChatChunk, None]],
        *,
        admit: Callable[[ModelDeployment], bool] | None = None,
    ) -> AsyncGenerator[AdapterChatChunk, None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + decision.deployment.timeout.total
        last_error: GatewayError | None = None
        previous = decision.deployment
        targets_used = 0
        for original in chain(decision, config):
            if targets_used >= config.fallback.max_chain:
                break
            target_used = False
            for attempt in range(1, config.retry.max_attempts + 1):
                # The prepared initial call keeps its snapshot; any subsequent call revalidates.
                initial = original is decision.deployment and ctx.attempt == 0
                deployment = original if initial else current_candidate(self.registry, original)
                if deployment is None:
                    break
                if not initial and admit is not None and not admit(deployment):
                    break
                if not initial and self.available is not None and not self.available(deployment):
                    break
                self.breakers.sync(self.registry.snapshot)
                permit = self.breakers.acquire(deployment)
                if permit is None:
                    break
                if loop.time() >= deadline:
                    self.breakers.finish(permit, None)
                    error = UpstreamTotalTimeoutError("request exhausted its total time budget")
                    self._failure(deployment, error)
                    raise error
                if not target_used:
                    targets_used += 1
                    target_used = True
                    if previous.id != deployment.id:
                        reason = last_error.code if last_error else "GW-4004"
                        FALLBACKS.labels(previous.id, deployment.id, reason).inc()
                        log_event(
                            log,
                            "fallback",
                            from_deployment=previous.id,
                            to_deployment=deployment.id,
                            reason=reason,
                        )
                elif last_error is not None:
                    RETRIES.labels(deployment.id, last_error.code).inc()
                    log_event(
                        log,
                        "retry",
                        deployment_id=deployment.id,
                        attempt=attempt,
                        reason=last_error.code,
                    )
                ctx.attempt += 1
                ctx.current_deployment = deployment
                ctx.deployment_id = deployment.id
                if deployment.id != decision.deployment.id:
                    ctx.fallback_from = decision.deployment.id
                previous = deployment
                attempt_deadline = min(deadline, loop.time() + deployment.timeout.total)
                terminal = False
                upstream_sec = 0.0
                try:
                    # Never leave a timeout context active across a yield to another task.
                    iterator = call(deployment)
                    async with aclosing(iterator):
                        while True:
                            try:
                                if loop.time() >= attempt_deadline:
                                    raise TimeoutError
                                started = loop.time()
                                try:
                                    async with asyncio.timeout_at(attempt_deadline):
                                        chunk = await anext(iterator)
                                finally:
                                    # Excludes downstream consumer backpressure between yields.
                                    upstream_sec += loop.time() - started
                            except StopAsyncIteration:
                                break
                            except TimeoutError as exc:
                                raise UpstreamTotalTimeoutError(
                                    "request exceeded total timeout"
                                ) from exc
                            if chunk.delta:
                                ctx.stream_started = True
                            if chunk.finish_reason is not None:
                                terminal = True
                            # Empty keepalives must not commit response headers or grow a buffer.
                            if chunk.delta or terminal:
                                yield chunk
                    if not terminal:
                        raise UpstreamProtocolError("upstream ended without a final chunk")
                except GatewayError as exc:
                    self.breakers.finish(permit, False if breaker_failure(exc) else None)
                    if self.observe is not None and breaker_failure(exc):
                        self.observe(deployment, False, upstream_sec)
                    self._failure(deployment, exc)
                    last_error = exc
                    if terminal or not can_retry(exc, output_received=ctx.stream_started):
                        raise
                except BaseException:
                    self.breakers.finish(permit, None)
                    raise
                else:
                    self.breakers.finish(permit, True)
                    if self.observe is not None:
                        self.observe(deployment, True, upstream_sec)
                    return
                if attempt < config.retry.max_attempts:
                    if not self.breakers.available(deployment):
                        break
                    if self.available is not None and not self.available(deployment):
                        break
                    try:
                        async with asyncio.timeout_at(deadline):
                            await asyncio.sleep(backoff_seconds(config.retry, attempt))
                    except TimeoutError as exc:
                        error = UpstreamTotalTimeoutError("retry wait exhausted total time budget")
                        self._failure(deployment, error)
                        raise error from exc
        if last_error is None:
            raise NoAvailableDeploymentError("no deployment admitted by routing or circuit breaker")
        if targets_used > 1:
            raise AllFallbacksFailedError("all attempted deployments failed") from last_error
        raise last_error

    @staticmethod
    def _failure(deployment: ModelDeployment, error: GatewayError) -> None:
        if error.code in TIMEOUT_KINDS:
            TIMEOUTS.labels(deployment.id, TIMEOUT_KINDS[error.code]).inc()
        log_event(log, "upstream_attempt_failed", deployment_id=deployment.id, reason=error.code)
