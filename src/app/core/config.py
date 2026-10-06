"""Application settings.

Every value can be overridden with an environment variable of the same name
(case-insensitive). In Kubernetes, non-secret values come from the
``scalebotai-config`` ConfigMap and secrets from ``scalebotai-secrets``.
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

INSECURE_DEFAULT_SECRET = "change-me-insecure-dev-secret-key-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- application -------------------------------------------------------
    app_name: str = "ScaleBotAI"
    app_env: str = "production"
    app_version: str = "1.0.0"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_file: str | None = None
    cors_origins: str = "*"

    # --- PostgreSQL -----------------------------------------------------------
    database_url: str | None = None
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "scalebotai"
    postgres_user: str = "scalebot"
    postgres_password: str = "change-me-postgres-password"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 10
    auto_create_tables: bool = True

    # --- Redis ------------------------------------------------------------------
    redis_url: str | None = None
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    # --- security ---------------------------------------------------------------
    jwt_secret_key: str = INSECURE_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    bcrypt_rounds: int = 12
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120

    # --- caching ----------------------------------------------------------------
    cache_ttl_seconds: int = 3600

    # --- chatbot ----------------------------------------------------------------
    chatbot_mode: Literal["rule", "llm"] = "rule"
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "tinyllama"
    ollama_timeout_seconds: float = 20.0

    # --- bootstrap admin (created at startup when ADMIN_PASSWORD is set) --------
    admin_username: str = "admin"
    admin_email: str = "admin@scalebotai.local"
    admin_password: str | None = None

    @field_validator("log_file", "database_url", "redis_url", "redis_password", "admin_password")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("chatbot_mode", "log_format", mode="before")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower() if isinstance(value, str) else value

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @property
    def redis_dsn(self) -> str:
        if self.redis_url:
            return self.redis_url
        auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def uses_insecure_secret(self) -> bool:
        return self.jwt_secret_key == INSECURE_DEFAULT_SECRET or len(self.jwt_secret_key) < 32


@lru_cache
def get_settings() -> Settings:
    return Settings()
