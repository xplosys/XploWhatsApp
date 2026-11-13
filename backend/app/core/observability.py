"""Sentry error reporting for the API and the workers. Nothing is sent without a DSN."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.logging import redact_value

_initialised = False


def _scrub(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    """Drop request bodies, cookies and auth headers, and redact secret-looking fields
    anywhere else (the same rules as the logs). Customer messages never leave."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in (
                    "authorization",
                    "cookie",
                    "x-hub-signature-256",
                    "x-razorpay-signature",
                    "stripe-signature",
                ):
                    headers[name] = "[redacted]"
    for key in ("extra", "contexts"):
        if isinstance(event.get(key), dict):
            event[key] = {k: redact_value(str(k), v) for k, v in event[key].items()}
    return event


def init_sentry(settings: Settings, component: str) -> bool:
    global _initialised
    if _initialised or settings.sentry_dsn is None:
        return _initialised
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=str(settings.environment),
        release=f"xplowhatsapp@{settings.app_version}",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=_scrub,  # type: ignore[arg-type]
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            CeleryIntegration(monitor_beat_tasks=False),
        ],
    )
    sentry_sdk.set_tag("component", component)
    _initialised = True
    return True
