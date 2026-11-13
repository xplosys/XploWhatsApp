"""Every route that changes something is either audited or listed here with the reason
it isn't. A new route fails this test until it is classified."""

from __future__ import annotations

import inspect
import pathlib
import re
from collections.abc import Iterator
from typing import Any

from fastapi.routing import APIRoute

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"

# Audited inside a service function the endpoint calls: route -> the action it records.
AUDITED_IN_SERVICE = {
    "POST /api/v1/auth/register": "user.registered",
    "POST /api/v1/auth/login": "auth.login",
    "POST /api/v1/auth/mfa/verify": "auth.login",
    "POST /api/v1/auth/verify-email": "user.email_verified",
    "POST /api/v1/auth/password/reset": "auth.password_reset",
    "POST /api/v1/auth/password/change": "auth.password_changed",
    "POST /api/v1/auth/mfa/enable": "auth.mfa_enabled",
    "POST /api/v1/auth/mfa/disable": "auth.mfa_disabled",
    "DELETE /api/v1/auth/me": "user.deleted",
    "POST /api/v1/auth/invitations/accept": "member.joined",
    "PUT /api/v1/tenants/current": "tenant.updated",
    "PATCH /api/v1/tenants/current/members/{membership_id}": "member.updated",
    "DELETE /api/v1/tenants/current/members/{membership_id}": "member.removed",
    "POST /api/v1/tenants/current/invitations": "invitation.created",
    "DELETE /api/v1/tenants/current/invitations/{invitation_id}": "invitation.revoked",
    "POST /api/v1/contacts/{contact_id}/erase": "contact.erased",
    "POST /api/v1/tenants/current/delete": "tenant.deletion_requested",
    "POST /api/v1/billing/checkout/razorpay/verify": "billing.subscription_started",
    "POST /api/v1/billing/checkout/stripe/confirm": "billing.subscription_started",
    "POST /api/v1/admin/tenants/{tenant_id}/suspend": "admin.tenant_suspended",
    "POST /api/v1/admin/tenants/{tenant_id}/reactivate": "admin.tenant_reactivated",
}

# Changes nothing worth an audit entry, or keeps its own record.
NOT_AUDITED = {
    "POST /api/v1/auth/refresh": "Renews the same sign-in; the refresh-token table keeps each rotation.",
    "POST /api/v1/auth/switch-tenant": "Moves the session to another business the person belongs to.",
    "POST /api/v1/auth/resend-verification": "Sends an email; rate limited.",
    "POST /api/v1/auth/password/forgot": "Sends an email only if the account exists; auditing would "
    "reveal which addresses have accounts. The reset itself is audited.",
    "POST /api/v1/auth/mfa/setup": "Prepares a secret that does nothing until mfa/enable (audited).",
    "POST /api/v1/whatsapp/onboarding/sessions": "Starts a short-lived sign-up session; the connection "
    "is audited as whatsapp.connected.",
    "POST /api/v1/whatsapp/accounts/{account_id}/health-check": "Refreshes status read from Meta.",
    "POST /api/v1/messages": "The message row records who sent it.",
    "POST /api/v1/conversations/{conversation_id}/messages": "The message row records who sent it.",
    "POST /api/v1/conversations/{conversation_id}/read": "Read state only.",
    "POST /api/v1/conversations/{conversation_id}/summary": "Asks the AI for a summary; changes no data.",
    "PATCH /api/v1/leads/{lead_id}": "Each change goes to the lead's append-only history with its author.",
    "POST /api/v1/leads/{lead_id}/notes": "Stored in the lead's append-only history with its author.",
    "POST /api/v1/ai/providers/{provider}/test": "Test call to the provider; stores the result only.",
    "POST /api/v1/ai/assistant/preview": "Preview reply; changes no data.",
    "POST /api/v1/knowledge/search": "Read-only search (POST for the query body).",
    "POST /api/v1/webhooks/whatsapp": "Signed Meta events, stored in the webhook inbox.",
    "POST /api/v1/webhooks/razorpay": "Signed gateway events, stored in billing_events.",
    "POST /api/v1/webhooks/stripe": "Signed gateway events, stored in billing_events.",
}

AUDIT_CALL = re.compile(r"\brecord\(|\b_audit\(|\baudit\.")


def walk(routes: list[Any], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):  # FastAPI's lazily included routers
            ctx = getattr(route, "include_context", None)
            yield from walk(route.original_router.routes, prefix + (getattr(ctx, "prefix", "") or ""))


def mutating_routes(app: Any) -> dict[str, APIRoute]:
    out = {}
    for path, route in walk(app.routes):
        for method in sorted(route.methods & MUTATING):
            out[f"{method} {path}"] = route
    return out


def test_every_mutating_route_is_audited_or_explained(app: Any) -> None:
    routes = mutating_routes(app)
    assert len(routes) > 80
    unclassified = []
    for key, route in routes.items():
        if key in AUDITED_IN_SERVICE or key in NOT_AUDITED:
            continue
        if not AUDIT_CALL.search(inspect.getsource(route.endpoint)):
            unclassified.append(key)
    assert unclassified == [], f"Add an audit record or classify these routes: {unclassified}"
    # The lists don't go stale.
    stale = (set(AUDITED_IN_SERVICE) | set(NOT_AUDITED)) - set(routes)
    assert stale == set(), f"Routes no longer exist: {stale}"
    assert not set(AUDITED_IN_SERVICE) & set(NOT_AUDITED)


def test_service_audit_actions_exist() -> None:
    source = "\n".join(p.read_text() for p in APP_DIR.rglob("*.py"))
    missing = sorted({a for a in AUDITED_IN_SERVICE.values() if f'"{a}"' not in source})
    assert missing == []
