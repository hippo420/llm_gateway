"""라우터 조립.

/v1 아래에 OpenAI 호환 API 를, 루트에 운영 엔드포인트를 둔다.
Spring AI 의 base-url 이 http://host:8080 이면 /v1/chat/completions 로 호출된다.
"""

from __future__ import annotations

from fastapi import APIRouter

from .routes import chat, health, models

# OpenAI 호환 API
v1_router = APIRouter(prefix="/v1")
v1_router.include_router(chat.router)
v1_router.include_router(models.router)

# 운영 엔드포인트 (인증 없음, prefix 없음)
ops_router = APIRouter()
ops_router.include_router(health.router)

# Phase 2: /metrics 는 prometheus_client 의 ASGI app 을 main 에서 mount 한다.
# Phase 4: admin_router (config 조회/reload/override) 가 여기 추가된다.
