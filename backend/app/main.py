"""FastAPI application factory. No business logic lives here."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.middleware import RequestContextMiddleware, UploadSizeLimitMiddleware
from app.core.observability import init_sentry
from app.core.redis import close_redis
from app.db.session import dispose_engines


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engines()
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    init_sentry(settings, "api")
    app = FastAPI(
        title="XploWhatsApp API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production_like else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production_like else "/openapi.json",
    )
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-Request-ID"],
            max_age=600,
        )
    # Largest knowledge file plus room for the form's other fields.
    app.add_middleware(
        UploadSizeLimitMiddleware, max_bytes=(settings.knowledge_max_file_mb + 1) * 1024 * 1024
    )
    app.add_middleware(
        RequestContextMiddleware,
        hsts_max_age=settings.hsts_max_age_seconds if settings.is_production_like else None,
    )
    # Outermost, so it times the whole request and sees the final status code.
    app.add_middleware(MetricsMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(api_router)
    return app


app = create_app()
