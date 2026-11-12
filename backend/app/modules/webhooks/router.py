"""Meta webhook endpoint: GET for subscription verification, POST for events."""

from __future__ import annotations

import hmac
import json

import structlog
from fastapi import APIRouter, Query, Request, Response, status

from app.core.config import get_settings
from app.core.errors import AppError, Forbidden, Unauthorized
from app.db.session import get_app_sessionmaker
from app.modules.webhooks.ingest import ingest
from app.modules.webhooks.signature import is_valid_signature
from app.workers.dispatch import PROCESS_WEBHOOK_EVENT, dispatch

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger(__name__)

MAX_BODY_BYTES = 3 * 1024 * 1024


class WebhookNotConfigured(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "webhook_not_configured"
    message = "WhatsApp webhooks are not configured on this server."


@router.get("/whatsapp", include_in_schema=False)
async def verify_subscription(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    expected = get_settings().meta_webhook_verify_token
    if expected is None:
        raise WebhookNotConfigured()
    if mode != "subscribe" or not hmac.compare_digest(token.encode(), expected.get_secret_value().encode()):
        raise Forbidden("Verification token mismatch.", code="verify_token_mismatch")
    return Response(content=challenge[:256], media_type="text/plain")


@router.post("/whatsapp", include_in_schema=False)
async def receive(request: Request) -> Response:
    secret = get_settings().meta_app_secret
    if secret is None:
        raise WebhookNotConfigured()
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise AppError("Payload too large.", code="payload_too_large")
    if not is_valid_signature(body, request.headers.get("x-hub-signature-256"), secret.get_secret_value()):
        log.warning("webhook_bad_signature", ip=request.client.host if request.client else None)
        raise Unauthorized("Invalid signature.", code="invalid_signature")
    try:
        payload = json.loads(body)
    except ValueError:
        # Signed but unreadable: acknowledge so Meta does not retry forever, and log it.
        log.warning("webhook_invalid_json")
        return Response(status_code=200)
    if not isinstance(payload, dict):
        return Response(status_code=200)

    async with get_app_sessionmaker()() as session, session.begin():
        stored = await ingest(session, payload)
    # After commit: hand events to workers. If this fails, the sweeper re-dispatches.
    for event in stored:
        await dispatch(PROCESS_WEBHOOK_EVENT, event.id, event.tenant_id)
    return Response(status_code=200)
