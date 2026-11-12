"""Typed application settings loaded from environment variables.

Every secret is required in production. In development and test, some secrets
(JWT signing key, encryption key) can be generated on the fly so the stack
starts without manual setup; a warning is logged when that happens.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class EmailBackend(StrEnum):
    CONSOLE = "console"
    MEMORY = "memory"
    SMTP = "smtp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    app_name: str = "XploWhatsApp"
    log_level: str = "INFO"
    log_json: bool = True

    # Public URLs
    frontend_base_url: str = "http://localhost:3000"
    api_base_url: str = "http://localhost:8000"
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Database. Three roles: owner (migrations), app (RLS enforced), admin (super admin only).
    database_url: SecretStr = SecretStr("postgresql+asyncpg://xplo_app:xplo_app@localhost:5432/xplowhatsapp")
    database_admin_url: SecretStr = SecretStr(
        "postgresql+asyncpg://xplo_admin:xplo_admin@localhost:5432/xplowhatsapp"
    )
    database_owner_url: SecretStr = SecretStr(
        "postgresql+asyncpg://xplo_owner:xplo_owner@localhost:5432/xplowhatsapp"
    )
    # Superuser URL is only used by the one-shot bootstrap command, never by the app.
    database_superuser_url: SecretStr | None = None
    database_pool_size: int = 10
    database_max_overflow: int = 10

    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    celery_broker_url: SecretStr | None = None
    celery_result_backend: SecretStr | None = None

    # Auth
    jwt_private_key_pem: SecretStr | None = None
    jwt_key_id: str = "k1"
    # JSON object {kid: public_pem} of retired keys still accepted for verification.
    jwt_previous_public_keys: SecretStr | None = None
    jwt_issuer: str = "xplowhatsapp"
    jwt_audience: str = "xplowhatsapp-api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    mfa_challenge_ttl_seconds: int = 300
    email_token_ttl_seconds: int = 60 * 60 * 24
    password_reset_ttl_seconds: int = 60 * 60
    invitation_ttl_seconds: int = 60 * 60 * 24 * 7
    refresh_cookie_name: str = "xplo_refresh"
    refresh_cookie_path: str = "/api/v1/auth"
    cookie_secure: bool = True
    cookie_domain: str | None = None
    login_max_failures: int = 5
    login_lockout_seconds: int = 900

    # Envelope encryption key-encryption keys: "v1:<base64 32 bytes>,v2:<...>"
    encryption_keys: SecretStr | None = None
    encryption_active_key_version: str = "v1"

    # Email
    email_backend: EmailBackend = EmailBackend.CONSOLE
    email_from: str = "XploWhatsApp <no-reply@xplowhatsapp.com>"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False

    # Rate limits (requests per minute)
    rate_limit_auth_per_minute: int = 20
    rate_limit_default_per_minute: int = 300

    sentry_dsn: SecretStr | None = None

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @property
    def is_production_like(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)

    @model_validator(mode="after")
    def _require_secrets_in_production(self) -> Settings:
        if self.is_production_like:
            missing = [
                name for name in ("jwt_private_key_pem", "encryption_keys") if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"Missing required settings in {self.environment}: {', '.join(missing)}")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true outside development")
            if self.email_backend != EmailBackend.SMTP:
                raise ValueError("EMAIL_BACKEND must be smtp outside development")
        return self

    @property
    def broker_url(self) -> str:
        return (self.celery_broker_url or self.redis_url).get_secret_value()

    @property
    def result_backend(self) -> str:
        return (self.celery_result_backend or self.redis_url).get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
