"""프로세스 기동 설정 (환경변수).

모델/엔드포인트 설정은 여기가 아니라 ``config/gateway.yaml`` 이다.
경계: 여기는 "프로세스를 어떻게 띄우는가", gateway.yaml 은 "무엇을 서빙하는가".

명세: docs/specs/config-spec.md
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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
    # 0 이면 파일 감시 비활성. Redis 동기화/TTL 확인은 별도 주기로 계속한다.
    config_reload_sec: int = Field(default=5, ge=0)
    config_redis_poll_sec: int = Field(default=5, ge=1)

    # --- auth ---
    # 비어 있으면 chat 인증 비활성, admin API 접근 거부.
    api_key: str = ""

    # --- logging ---
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    # true 면 프롬프트 원문이 로그에 남는다. 운영에서는 반드시 false.
    log_prompt: bool = False

    # --- observability (Phase 2) ---
    metrics_enabled: bool = True

    # Completed offline evaluation summaries, read by admin API and quality metrics.
    evaluation_results_path: Path = Path("operations/evaluations")

    # --- rule-based diagnosis (Phase 3) ---
    diagnosis_enabled: bool = False
    diagnosis_config_path: Path = Path("config/diagnosis.yaml")

    # --- dynamic config store (Phase 4) ---
    redis_url: str = ""

    # --- human-approved dynamic policies (Phase 9) ---
    policy_enabled: bool = False
    policy_config_path: Path = Path("config/policies.yaml")

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
