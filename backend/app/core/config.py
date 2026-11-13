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


class TaskDispatch(StrEnum):
    CELERY = "celery"
    MEMORY = "memory"  # tests: record jobs instead of sending them to the broker


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

    # Background jobs
    task_dispatch: TaskDispatch = TaskDispatch.CELERY

    # Meta WhatsApp Cloud API. Every Meta-defined value is configuration, not code:
    # re-check against Meta's changelog when upgrading.
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    meta_webhook_verify_token: SecretStr | None = None
    meta_embedded_signup_config_id: str | None = None
    # Embedded Signup flow version sent in FB.login extras ("v4" uses products from the
    # Login for Business configuration; "v5" adds WhatsApp Business app onboarding).
    meta_embedded_signup_version: str = "v4"
    meta_graph_api_version: str = "v25.0"
    meta_graph_base_url: str = "https://graph.facebook.com"
    meta_http_timeout_seconds: float = 15.0
    # Customer-service window for free-form messages, per Meta policy.
    whatsapp_service_window_hours: int = 24
    whatsapp_opt_out_keywords: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["stop", "unsubscribe", "stop all", "opt out", "optout"]
    )
    whatsapp_opt_in_keywords: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["start", "subscribe", "opt in", "unstop"]
    )
    webhook_max_attempts: int = 8
    outbox_max_attempts: int = 8

    # AI providers. Platform keys are XploSys's own and are used only for businesses whose
    # plan allows it (limit "platform_ai_keys"); businesses can always add their own key.
    # Model IDs and prices are data in the model catalogue, never settings or code.
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com"
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    deepseek_base_url: str = "https://api.deepseek.com"
    ai_http_timeout_seconds: float = 30.0
    ai_max_output_tokens: int = 2048
    # Attempts per provider before moving to the next one in the fallback chain.
    ai_attempts_per_provider: int = 2
    # Wait this long after an inbound message so a burst of messages gets one reply.
    ai_reply_debounce_seconds: float = 2.0
    ai_history_messages: int = 20
    ai_history_max_chars: int = 12000

    # Object storage: the S3 API only (MinIO in development). The public endpoint is the
    # one browsers can reach, used for short-lived download links.
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_bucket: str = "xplowhatsapp-private"
    s3_presign_seconds: int = 300

    # Virus scanning with clamd over TCP. Required outside development.
    clamav_host: str | None = None
    clamav_port: int = 3310
    clamav_timeout_seconds: float = 60.0

    # Knowledge base. Plan limits (documents, storage) come from the subscription plan.
    knowledge_max_file_mb: int = 20
    knowledge_max_extracted_chars: int = 2_000_000
    knowledge_max_chunks_per_document: int = 3000
    knowledge_chunk_tokens: int = 600
    knowledge_chunk_overlap_tokens: int = 80
    knowledge_embedding_batch: int = 96
    knowledge_url_timeout_seconds: float = 15.0
    knowledge_url_max_mb: int = 5
    knowledge_url_max_redirects: int = 3
    # Only for local testing against a server on this machine. Never in production.
    knowledge_url_allow_private: bool = False

    @field_validator(
        "cors_allowed_origins", "whatsapp_opt_out_keywords", "whatsapp_opt_in_keywords", mode="before"
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @property
    def meta_embedded_signup_enabled(self) -> bool:
        return bool(self.meta_app_id and self.meta_app_secret and self.meta_embedded_signup_config_id)

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
            if not self.clamav_host:
                raise ValueError("CLAMAV_HOST is required outside development: uploads must be scanned")
            if self.knowledge_url_allow_private:
                raise ValueError("KNOWLEDGE_URL_ALLOW_PRIVATE must be false outside development")
            if self.email_backend != EmailBackend.SMTP:
                raise ValueError("EMAIL_BACKEND must be smtp outside development")
            if self.task_dispatch != TaskDispatch.CELERY:
                raise ValueError("TASK_DISPATCH must be celery outside development")
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
