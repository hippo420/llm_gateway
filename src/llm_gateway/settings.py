"""프로세스 기동 설정 (환경변수).

모델/엔드포인트 설정은 여기가 아니라 ``config/gateway.yaml`` 이다.
경계: 여기는 "프로세스를 어떻게 띄우는가", gateway.yaml 은 "무엇을 서빙하는가".

명세: docs/specs/config-spec.md
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- server ---
    host: str = "0.0.0.0"
    port: int = 8080

    # --- model registry ---
    config_path: Path = Path("config/gateway.yaml")
    # 0 이면 파일 감시 비활성 (Phase 4 에서 사용)
    config_reload_sec: int = 0

    # --- auth ---
    # 비어 있으면 인증 비활성
    api_key: str = ""

    # --- logging ---
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    # true 면 프롬프트 원문이 로그에 남는다. 운영에서는 반드시 false.
    log_prompt: bool = False

    # --- observability (Phase 2) ---
    metrics_enabled: bool = True

    # --- dynamic config store (Phase 4) ---
    redis_url: str = ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 전역 설정 싱글턴.

    TODO: 테스트에서 override 할 수 있도록 FastAPI dependency_overrides 와 함께 쓸 것.
          (`get_settings.cache_clear()` 로 초기화 가능)
    """
    return Settings()
