from fastapi import APIRouter

from app.modules.admin.router import router as admin_router
from app.modules.ai.admin_router import router as ai_admin_router
from app.modules.ai.router import router as ai_router
from app.modules.analytics.router import router as analytics_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.billing.admin_router import router as billing_admin_router
from app.modules.billing.router import router as billing_router
from app.modules.billing.router import webhooks as billing_webhooks_router
from app.modules.conversations.contacts_router import router as contacts_router
from app.modules.conversations.inbox_router import router as inbox_router
from app.modules.conversations.router import router as conversations_router
from app.modules.conversations.unanswered_router import router as unanswered_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.leads.router import router as leads_router
from app.modules.tenants.router import router as tenants_router
from app.modules.webhooks.router import router as webhooks_router
from app.modules.whatsapp.router import router as whatsapp_router

api_router = APIRouter(prefix="/api/v1")
for r in (
    auth_router,
    tenants_router,
    billing_router,
    billing_admin_router,
    audit_router,
    admin_router,
    whatsapp_router,
    conversations_router,
    inbox_router,
    contacts_router,
    leads_router,
    ai_router,
    ai_admin_router,
    unanswered_router,
    knowledge_router,
    analytics_router,
    webhooks_router,
    billing_webhooks_router,
):
    api_router.include_router(r)
