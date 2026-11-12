from fastapi import APIRouter

from app.modules.admin.router import router as admin_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.billing.router import router as billing_router
from app.modules.conversations.router import router as conversations_router
from app.modules.tenants.router import router as tenants_router
from app.modules.webhooks.router import router as webhooks_router
from app.modules.whatsapp.router import router as whatsapp_router

api_router = APIRouter(prefix="/api/v1")
for r in (
    auth_router,
    tenants_router,
    billing_router,
    audit_router,
    admin_router,
    whatsapp_router,
    conversations_router,
    webhooks_router,
):
    api_router.include_router(r)
