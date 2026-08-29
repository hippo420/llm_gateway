"""FastAPI application factory / entrypoint.

실행:
    uvicorn llm_gateway.main:app --host 0.0.0.0 --port 8080
    python -m llm_gateway.main
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .adapters.factory import AdapterFactory
from .api.router import ops_router, v1_router
from .core.context import current_request_id
from .core.errors import GatewayError, InternalError, InvalidRequestError
from .core.logging import configure_logging, log_event
from .middleware.access_log import AccessLogMiddleware
from .middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from .registry.loader import YamlConfigSource
from .registry.models import ModelRegistry
from .service.chat_service import ChatService
from .settings import Settings, get_settings

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """기동 / 종료 훅. Spring 의 @PostConstruct / @PreDestroy 자리다."""
    settings: Settings = app.state.settings
    configure_logging(settings)

    # 설정 로드 실패는 여기서 예외로 올린다 = 기동 중단.
    # 잘못된 설정으로 뜨는 것보다 안 뜨는 게 낫다. (Phase 4 의 reload 실패와는 다르게 다룬다)
    source = YamlConfigSource(settings.config_path)
    snapshot = source.load()

    app.state.config_source = source
    app.state.registry = ModelRegistry(snapshot)
    app.state.adapters = AdapterFactory()
    app.state.chat_service = ChatService(app.state.registry, app.state.adapters)

    log_event(
        log,
        "gateway_started",
        config_path=str(settings.config_path),
        config_version=snapshot.version,
        models=len(snapshot.models),
        deployments=[d.id for d in app.state.registry.all_deployments()],
        auth_enabled=settings.auth_enabled,
    )

    # Phase 4: 여기서 config watcher task 를 띄운다.
    yield

    # 빠뜨리면 uvicorn 종료가 매달린다.
    await app.state.adapters.close_all()
    log_event(log, "gateway_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """앱 조립. 테스트가 설정을 바꿔 여러 번 만들 수 있도록 factory 로 둔다."""
    settings = settings or get_settings()

    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    # lifespan 이 읽어야 하므로 조립 시점에 먼저 넣는다.
    app.state.settings = settings

    app.include_router(v1_router)
    app.include_router(ops_router)

    # Starlette 은 **마지막에 add 한 것이 가장 바깥**이다.
    # request_id 가 access log 보다 바깥에 있어야 로그에 id 가 찍힌다.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.add_exception_handler(GatewayError, gateway_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # Phase 2: settings.metrics_enabled 면 app.mount("/metrics", metrics_asgi_app()).

    return app


async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    """GatewayError -> OpenAI 호환 에러 응답."""
    request_id = _request_id_of(request)

    log_event(
        log,
        "request_failed",
        level=logging.WARNING if exc.http_status < 500 else logging.ERROR,
        path=request.url.path,
        status=exc.http_status,
        code=exc.code,
        error_type=str(exc.error_type),
        # detail 은 로그에만 남긴다. 응답 body 에 넣으면 내부 정보가 샌다.
        detail=exc.detail,
    )

    # Phase 2: record_error(exc, ...) 를 여기서 부른다.
    return JSONResponse(
        status_code=exc.http_status,
        content=exc.to_error_body(request_id),
        # 예외 경로에서 미들웨어가 헤더를 못 붙이는 구성이 되기 쉽다. 여기서 한 번 더 챙긴다.
        headers={REQUEST_ID_HEADER: request_id} if request_id else None,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Pydantic 검증 실패 -> GW-4000 invalid_request.

    FastAPI 기본 422 응답 대신 Gateway 에러 형식으로 통일한다.
    (Spring 쪽에서 에러 파싱 코드를 하나만 두게 하기 위함)
    """
    fields: list[str] = []
    if isinstance(exc, RequestValidationError):
        # 검증 에러 상세에는 요청 body 조각(= 프롬프트)이 들어간다.
        # **필드 이름만 남기고 값은 버린다.**
        fields = [".".join(str(p) for p in err.get("loc", ())) for err in exc.errors()]

    message = "invalid request body"
    if fields:
        message = f"invalid request body: {', '.join(fields)}"

    return await gateway_error_handler(
        request, InvalidRequestError(message, detail={"fields": fields})
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """예상 못 한 예외 -> GW-5010 internal_error.

    스택트레이스는 로그에만, 응답에는 일반 메시지만 담는다.
    """
    log.exception("unhandled error", extra={"event": "unhandled_error"})
    return await gateway_error_handler(
        request,
        InternalError(
            "internal server error",
            detail={"exception": exc.__class__.__name__},
        ),
    )


def _request_id_of(request: Request) -> str:
    """미들웨어가 만든 컨텍스트에서 request_id 를 꺼낸다."""
    ctx = getattr(request.state, "ctx", None)
    if ctx is not None:
        return str(ctx.request_id)
    return current_request_id() or ""


# uvicorn 이 import 하는 ASGI app.
app = create_app()


def main() -> None:
    """python -m llm_gateway.main 진입점."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "llm_gateway.main:app",
        host=settings.host,
        port=settings.port,
        # uvicorn 기본 access log 는 request_id 를 모른다. AccessLogMiddleware 로 대체.
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
