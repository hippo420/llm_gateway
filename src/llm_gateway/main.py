"""FastAPI application factory / entrypoint.

실행:
    uvicorn llm_gateway.main:app --host 0.0.0.0 --port 8080
    python -m llm_gateway.main
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .core.errors import GatewayError
from .settings import Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """기동 / 종료 훅.

    TODO: 구현.

    startup:
      1. settings = get_settings(); configure_logging(settings)
      2. source = YamlConfigSource(settings.config_path)
      3. snapshot = source.load()
         - 실패하면 **기동을 중단한다.** 잘못된 설정으로 뜨는 것보다 안 뜨는 게 낫다.
           (reload 실패와는 다르게 다룬다 - Phase 4)
      4. app.state.settings / registry / adapters 세팅
      5. (Phase 4) config watcher task 시작
      6. 기동 로그: 모델 수, deployment 목록, 버전

    shutdown:
      - await app.state.adapters.close_all()
        빠뜨리면 uvicorn 종료가 매달린다.
      - (Phase 4) watcher task 취소
    """
    raise NotImplementedError
    yield  # pragma: no cover


def create_app(settings: Settings | None = None) -> FastAPI:
    """앱 조립.

    TODO: 구현.

    미들웨어 등록 순서가 중요하다. Starlette 은 **마지막에 add 한 것이 가장 바깥**이다.
    request_id 가 access log 보다 바깥에 있어야 로그에 id 가 찍힌다:

        app.add_middleware(AccessLogMiddleware)     # 안쪽
        app.add_middleware(RequestIdMiddleware)     # 바깥쪽  <- 나중에 add

    그 외:
      - app.include_router(v1_router), app.include_router(ops_router)
      - GatewayError / RequestValidationError / Exception 핸들러 등록
      - settings.metrics_enabled 면 app.mount("/metrics", metrics_asgi_app())  [Phase 2]
      - docs_url 은 운영에서 끌지 결정할 것
    """
    raise NotImplementedError


async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    """GatewayError -> OpenAI 호환 에러 응답.

    TODO: 구현.
      - exc.to_error_body(request_id) 로 body 생성
      - status_code = exc.http_status
      - **응답 헤더에 X-Request-Id 를 반드시 넣는다.**
        예외 경로에서 미들웨어가 헤더를 못 붙이는 구성이 되기 쉽다. 여기서 한 번 더 챙긴다.
      - exc.detail 은 로그에만. 응답 body 에 넣지 않는다 (내부 정보 노출).
      - (Phase 2) record_error() 호출
    """
    raise NotImplementedError


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Pydantic 검증 실패 -> GW-4000 invalid_request.

    TODO: 구현. FastAPI 기본 422 응답 대신 Gateway 에러 형식으로 통일한다.
          (Spring 쪽에서 에러 파싱 코드를 하나만 두게 하기 위함)

    주의: 검증 에러 상세에는 요청 body 조각이 들어갈 수 있다 = 프롬프트가 샌다.
          필드 이름만 남기고 값은 제거할 것.
    """
    raise NotImplementedError


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """예상 못 한 예외 -> GW-5010 internal_error.

    TODO: 구현. 스택트레이스는 로그에만, 응답에는 일반 메시지만.
    """
    raise NotImplementedError


# uvicorn 이 import 하는 ASGI app.
# create_app() 을 구현하기 전까지는 이 줄에서 NotImplementedError 가 난다 (정상).
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
