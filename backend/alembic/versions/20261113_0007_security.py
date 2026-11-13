"""Security and privacy: business MFA requirement, message retention, business deletion
with a grace period, account deletion, data exports, and the lookups the privacy
sweeper needs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13 18:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.rls import disable_tenant_rls, enable_tenant_rls, grant_execute_to_app
from app.db.roles import APP_ROLE, DEFINER_ROLE

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_FUNCTIONS = {
    # Deleted businesses whose grace period is over and that have not been purged yet.
    "app_tenants_due_purge(integer)": """
        CREATE FUNCTION app_tenants_due_purge(p_limit integer)
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT id FROM tenants
            WHERE deleted_at IS NOT NULL AND purged_at IS NULL AND purge_after <= now()
            ORDER BY purge_after
            LIMIT least(p_limit, 100)
        $$
    """,
    # Live businesses that keep messages for a limited number of days.
    "app_tenants_with_retention(integer)": """
        CREATE FUNCTION app_tenants_with_retention(p_limit integer)
        RETURNS TABLE (tenant_id uuid, days integer)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT id, message_retention_days FROM tenants
            WHERE message_retention_days IS NOT NULL AND deleted_at IS NULL
            ORDER BY id
            LIMIT least(p_limit, 10000)
        $$
    """,
    # Exports to expire (ready and past their date) or to recover (stuck for an hour).
    "app_data_exports_due(integer)": """
        CREATE FUNCTION app_data_exports_due(p_limit integer)
        RETURNS TABLE (tenant_id uuid, export_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT tenant_id, id FROM data_exports
            WHERE (status = 'ready' AND expires_at <= now())
               OR (status IN ('queued', 'running') AND updated_at < now() - interval '1 hour')
            LIMIT least(p_limit, 1000)
        $$
    """,
    # Businesses (not deleted) where this person is the only active owner. An account in
    # that position can't be deleted until ownership is handed over or the business deleted.
    "app_sole_owner_tenants(uuid)": """
        CREATE FUNCTION app_sole_owner_tenants(p_user_id uuid)
        RETURNS TABLE (tenant_id uuid, tenant_name text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT t.id, t.name::text
            FROM tenant_memberships m JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = p_user_id AND m.role = 'owner' AND m.status = 'active'
              AND t.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tenant_memberships o
                  WHERE o.tenant_id = m.tenant_id AND o.user_id <> p_user_id
                    AND o.role = 'owner' AND o.status = 'active'
              )
        $$
    """,
    # Removes a deleted account from every business it belonged to. Refuses while the
    # account is still active, so it can only follow the account deletion itself.
    "app_remove_user_memberships(uuid)": """
        CREATE FUNCTION app_remove_user_memberships(p_user_id uuid)
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = p_user_id AND deleted_at IS NOT NULL) THEN
                RAISE EXCEPTION 'account is not deleted';
            END IF;
            RETURN QUERY DELETE FROM tenant_memberships m WHERE m.user_id = p_user_id RETURNING m.tenant_id;
        END
        $$
    """,
    # Raw WhatsApp webhook payloads hold phone numbers and message text. Once handled,
    # they are only kept for a short debugging window. Returns the number removed.
    "app_purge_webhook_events(integer)": """
        CREATE FUNCTION app_purge_webhook_events(p_days integer)
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE n integer;
        BEGIN
            IF p_days < 1 THEN
                RAISE EXCEPTION 'retention must be at least one day';
            END IF;
            DELETE FROM webhook_events
            WHERE status <> 'pending' AND received_at < now() - make_interval(days => p_days);
            GET DIAGNOSTICS n = ROW_COUNT;
            RETURN n;
        END
        $$
    """,
}


def upgrade() -> None:
    op.add_column("tenants", sa.Column("require_mfa", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("tenants", sa.Column("message_retention_days", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_tenants_message_retention_days_range"),
        "tenants",
        "message_retention_days IS NULL OR message_retention_days BETWEEN 30 AND 3650",
    )
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "data_exports",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("object_key", sa.String(length=300), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed', 'expired')",
            name=op.f("ck_data_exports_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name=op.f("fk_data_exports_requested_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_data_exports_tenant_id_tenants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_exports")),
    )
    op.create_index(
        "ix_data_exports_tenant_created", "data_exports", ["tenant_id", "created_at"], unique=False
    )
    for stmt in enable_tenant_rls("data_exports"):
        op.execute(stmt)
    # Exports are kept as a record; the sweeper only marks them expired.
    op.execute(f'REVOKE DELETE ON data_exports FROM "{APP_ROLE}"')

    op.execute(f'GRANT SELECT ON data_exports TO "{DEFINER_ROLE}"')
    op.execute(f'GRANT DELETE ON webhook_events TO "{DEFINER_ROLE}"')
    op.execute(f'GRANT SELECT ON users TO "{DEFINER_ROLE}"')
    op.execute(f'GRANT DELETE ON tenant_memberships TO "{DEFINER_ROLE}"')
    for signature, ddl in DEFINER_FUNCTIONS.items():
        op.execute(ddl)
        op.execute(f'ALTER FUNCTION {signature} OWNER TO "{DEFINER_ROLE}"')
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(grant_execute_to_app(signature))


def downgrade() -> None:
    for signature in DEFINER_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(f'REVOKE DELETE ON webhook_events FROM "{DEFINER_ROLE}"')
    op.execute(f'REVOKE DELETE ON tenant_memberships FROM "{DEFINER_ROLE}"')
    op.execute(f'REVOKE SELECT ON users FROM "{DEFINER_ROLE}"')  # noqa: S608
    op.execute(f'REVOKE SELECT ON data_exports FROM "{DEFINER_ROLE}"')  # noqa: S608
    for stmt in disable_tenant_rls("data_exports"):
        op.execute(stmt)
    op.drop_index("ix_data_exports_tenant_created", table_name="data_exports")
    op.drop_table("data_exports")
    op.drop_column("users", "deleted_at")
    op.drop_constraint(op.f("ck_tenants_message_retention_days_range"), "tenants", type_="check")
    op.drop_column("tenants", "purged_at")
    op.drop_column("tenants", "purge_after")
    op.drop_column("tenants", "message_retention_days")
    op.drop_column("tenants", "require_mfa")
