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
from app.modules.billing.models import (
    BillingEvent,
    BillingProfile,
    Checkout,
    Invoice,
    InvoiceSequence,
    PlanPrice,
    Subscription,
    SubscriptionPlan,
    Transaction,
)
from app.modules.conversations.models import (
    Contact,
    Conversation,
    ConversationNote,
    ConversationSummary,
    HandoverSession,
    Message,
    UnansweredQuestion,
)
from app.modules.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeEmbedding,
    KnowledgeSettings,
)
from app.modules.leads.models import Lead, LeadActivity
from app.modules.privacy.models import DataExport
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
    "BillingEvent",
    "BillingProfile",
    "Checkout",
    "Contact",
    "Conversation",
    "ConversationNote",
    "ConversationSummary",
    "DataExport",
    "HandoverSession",
    "Invoice",
    "InvoiceSequence",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeDocumentVersion",
    "KnowledgeEmbedding",
    "KnowledgeSettings",
    "Lead",
    "LeadActivity",
    "Message",
    "MessageTemplate",
    "OutboxJob",
    "PlanPrice",
    "RefreshToken",
    "Subscription",
    "SubscriptionPlan",
    "Tenant",
    "TenantInvitation",
    "TenantMembership",
    "Transaction",
    "UnansweredQuestion",
    "UsageRecord",
    "User",
    "UserActionToken",
    "WebhookEvent",
    "WhatsAppAccount",
    "WhatsAppCredential",
    "WhatsAppNumber",
]
