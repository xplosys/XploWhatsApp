"""Imports every model so Alembic and tests see the full metadata."""

from app.modules.ai.models import (
    AIModel,
    AIModelPrice,
    AIProviderConfig,
    Assistant,
    AssistantVersion,
    UsageRecord,
)
from app.modules.audit.models import AuditLog
from app.modules.auth.models import RefreshToken, User, UserActionToken
from app.modules.billing.models import Subscription, SubscriptionPlan
from app.modules.conversations.models import Contact, Conversation, Message
from app.modules.tenants.models import Tenant, TenantInvitation, TenantMembership
from app.modules.webhooks.models import OutboxJob, WebhookEvent
from app.modules.whatsapp.models import MessageTemplate, WhatsAppAccount, WhatsAppCredential, WhatsAppNumber

__all__ = [
    "AIModel",
    "AIModelPrice",
    "AIProviderConfig",
    "Assistant",
    "AssistantVersion",
    "AuditLog",
    "Contact",
    "Conversation",
    "Message",
    "MessageTemplate",
    "OutboxJob",
    "RefreshToken",
    "Subscription",
    "SubscriptionPlan",
    "Tenant",
    "TenantInvitation",
    "TenantMembership",
    "UsageRecord",
    "User",
    "UserActionToken",
    "WebhookEvent",
    "WhatsAppAccount",
    "WhatsAppCredential",
    "WhatsAppNumber",
]
