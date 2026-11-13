"""Role-based access control: one permission matrix for the whole product.

Permissions for later phases are declared now so the matrix is reviewed once
and every endpoint added later just names the permission it needs.
"""

from __future__ import annotations

from enum import StrEnum

from app.modules.tenants.models import TenantRole


class Permission(StrEnum):
    TENANT_READ = "tenant:read"
    TENANT_UPDATE = "tenant:update"
    MEMBERS_READ = "members:read"
    MEMBERS_INVITE = "members:invite"
    MEMBERS_MANAGE = "members:manage"
    AUDIT_READ = "audit:read"
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"
    WHATSAPP_READ = "whatsapp:read"
    WHATSAPP_MANAGE = "whatsapp:manage"
    AI_CONFIGURE = "ai:configure"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_MANAGE = "knowledge:manage"
    CONVERSATIONS_READ = "conversations:read"
    CONVERSATIONS_REPLY = "conversations:reply"
    CONVERSATIONS_ASSIGN = "conversations:assign"
    CONTACTS_MANAGE = "contacts:manage"
    LEADS_MANAGE = "leads:manage"
    ANALYTICS_READ = "analytics:read"
    # Erase a customer's data on request (DPDP Act / GDPR right to erasure).
    PRIVACY_MANAGE = "privacy:manage"
    # Business-wide security settings, full data export and deleting the business.
    SECURITY_MANAGE = "security:manage"


_AGENT = {
    Permission.TENANT_READ,
    Permission.MEMBERS_READ,
    Permission.WHATSAPP_READ,
    Permission.KNOWLEDGE_READ,
    Permission.CONVERSATIONS_READ,
    Permission.CONVERSATIONS_REPLY,
    Permission.CONTACTS_MANAGE,
    Permission.LEADS_MANAGE,
}
_SUPERVISOR = _AGENT | {Permission.CONVERSATIONS_ASSIGN, Permission.ANALYTICS_READ}
_ADMIN = _SUPERVISOR | {
    Permission.TENANT_UPDATE,
    Permission.MEMBERS_INVITE,
    Permission.MEMBERS_MANAGE,
    Permission.AUDIT_READ,
    Permission.BILLING_READ,
    Permission.WHATSAPP_MANAGE,
    Permission.AI_CONFIGURE,
    Permission.KNOWLEDGE_MANAGE,
    Permission.PRIVACY_MANAGE,
}
_OWNER = _ADMIN | {Permission.BILLING_MANAGE, Permission.SECURITY_MANAGE}

ROLE_PERMISSIONS: dict[TenantRole, frozenset[Permission]] = {
    TenantRole.AGENT: frozenset(_AGENT),
    TenantRole.SUPERVISOR: frozenset(_SUPERVISOR),
    TenantRole.ADMIN: frozenset(_ADMIN),
    TenantRole.OWNER: frozenset(_OWNER),
}

ROLE_RANK: dict[TenantRole, int] = {
    TenantRole.AGENT: 1,
    TenantRole.SUPERVISOR: 2,
    TenantRole.ADMIN: 3,
    TenantRole.OWNER: 4,
}


def has_permission(role: TenantRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def can_assign_role(actor: TenantRole, target_role: TenantRole) -> bool:
    """Owners can grant any role except owner (ownership transfer is a separate flow);
    admins can grant supervisor and agent."""
    if target_role == TenantRole.OWNER or actor not in (TenantRole.OWNER, TenantRole.ADMIN):
        return False
    if actor == TenantRole.OWNER:
        return True
    return ROLE_RANK[actor] > ROLE_RANK[target_role]


def can_manage_member(actor: TenantRole, member_role: TenantRole) -> bool:
    if actor not in (TenantRole.OWNER, TenantRole.ADMIN):
        return False
    if actor == TenantRole.OWNER:
        return True
    return ROLE_RANK[actor] > ROLE_RANK[member_role]
