from app.modules.rbac.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    can_assign_role,
    can_manage_member,
    has_permission,
)
from app.modules.tenants.models import TenantRole as R


def test_roles_are_strictly_nested() -> None:
    assert (
        ROLE_PERMISSIONS[R.AGENT]
        < ROLE_PERMISSIONS[R.SUPERVISOR]
        < ROLE_PERMISSIONS[R.ADMIN]
        < ROLE_PERMISSIONS[R.OWNER]
    )


def test_key_permissions() -> None:
    assert has_permission(R.AGENT, Permission.CONVERSATIONS_REPLY)
    assert not has_permission(R.AGENT, Permission.TENANT_UPDATE)
    assert not has_permission(R.SUPERVISOR, Permission.MEMBERS_INVITE)
    assert has_permission(R.ADMIN, Permission.WHATSAPP_MANAGE)
    assert not has_permission(R.ADMIN, Permission.BILLING_MANAGE)
    assert has_permission(R.OWNER, Permission.BILLING_MANAGE)


def test_role_assignment_hierarchy() -> None:
    assert not can_assign_role(R.OWNER, R.OWNER)
    assert can_assign_role(R.OWNER, R.ADMIN)
    assert not can_assign_role(R.ADMIN, R.ADMIN)
    assert can_assign_role(R.ADMIN, R.SUPERVISOR)
    assert not can_assign_role(R.SUPERVISOR, R.AGENT)


def test_member_management_hierarchy() -> None:
    assert can_manage_member(R.OWNER, R.OWNER)
    assert not can_manage_member(R.ADMIN, R.OWNER)
    assert not can_manage_member(R.ADMIN, R.ADMIN)
    assert can_manage_member(R.ADMIN, R.AGENT)
    assert not can_manage_member(R.SUPERVISOR, R.AGENT)
