"""Personal data tools for India's DPDP Act 2023 and the GDPR.

- Erasing one customer (a WhatsApp contact) and everything recorded about them.
- A full export of a business's data, built by a worker into a private zip file.
- Deleting a business: access ends at once, data is purged after a grace period.
- Message retention: messages older than the business's chosen limit are removed.

The business is the data fiduciary (controller) for its customers' data; XploSys is
its processor. These tools are how the business answers its customers' requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import Table, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict
from app.db.base import Base
from app.db.session import (
    admin_transaction,
    app_transaction,
    get_app_sessionmaker,
    run_after_commit,
    set_tenant_context,
)
from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.audit.service import RequestMeta
from app.modules.billing.models import Subscription, SubscriptionStatus
from app.modules.conversations.models import Contact, Conversation, Message
from app.modules.leads.models import Lead
from app.modules.privacy.models import DataExport, ExportStatus
from app.modules.storage import s3
from app.modules.tenants.models import Tenant

log = structlog.get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def tenant_tables() -> dict[str, str]:
    """{table: tenant column} for every tenant-owned table, in dependency order."""
    import app.db.models  # noqa: F401  (every model registered)

    scoped = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if getattr(mapper.class_, "__tenant_scoped__", False) and isinstance(mapper.local_table, Table)
    }
    return {
        t.name: ("id" if t.name == "tenants" else "tenant_id")
        for t in Base.metadata.sorted_tables
        if t.name in scoped
    }


# ---------------------------------------------------------------------- contact erasure


def phone_fingerprint(tenant_id: uuid.UUID, phone: str | None) -> str | None:
    """A one-way fingerprint of the number, kept in the audit log so the business can
    later show that a given number was erased without storing the number itself."""
    if not phone:
        return None
    return hashlib.sha256(f"{tenant_id}:{phone}".encode()).hexdigest()[:32]


async def erase_contact(
    session: AsyncSession, tenant_id: uuid.UUID, contact: Contact, actor_id: uuid.UUID, meta: RequestMeta
) -> dict[str, int]:
    """Delete a customer with their conversations, messages, notes, summaries and leads.
    Database cascades remove the dependent rows; the counts are recorded for the audit."""
    conversation_ids = select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, Conversation.contact_id == contact.id
    )
    counts = {
        "conversations": int(
            await session.scalar(select(func.count()).select_from(conversation_ids.subquery())) or 0
        ),
        "messages": int(
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.tenant_id == tenant_id, Message.conversation_id.in_(conversation_ids))
            )
            or 0
        ),
        "leads": int(
            await session.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.tenant_id == tenant_id, Lead.contact_id == contact.id)
            )
            or 0
        ),
    }
    fingerprint = phone_fingerprint(tenant_id, contact.phone_e164 or contact.wa_id)
    await session.delete(contact)
    await session.flush()
    await audit.record(
        session,
        "contact.erased",
        tenant_id=tenant_id,
        actor_user_id=actor_id,
        meta=meta,
        target_type="contact",
        target_id=contact.id,
        details={**counts, "phone_fingerprint": fingerprint},
    )
    return counts


# ---------------------------------------------------------------------- data export

# Raw material that is either secret, rebuilt from other tables, or internal plumbing.
EXPORT_SKIP_TABLES = {
    "whatsapp_credentials",
    "knowledge_chunks",
    "knowledge_embeddings",
    "webhook_events",
    "webhook_outbox",
    "billing_events",
    "billing_checkouts",
    "data_exports",
}
EXPORT_SKIP_COLUMNS = {"ciphertext", "api_key_ciphertext", "token_hash", "embedding", "mfa_secret_enc"}
EXPORT_BATCH = 2000


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    return str(value)


async def request_export(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> DataExport:
    settings = get_settings()
    since = utcnow() - timedelta(days=1)
    recent = await session.scalar(
        select(func.count())
        .select_from(DataExport)
        .where(DataExport.tenant_id == tenant_id, DataExport.created_at >= since)
    )
    if (recent or 0) >= settings.data_exports_per_day:
        raise Conflict(
            f"A business can request {settings.data_exports_per_day} exports a day. Try again tomorrow.",
            code="export_limit_reached",
        )
    running = await session.scalar(
        select(DataExport.id).where(
            DataExport.tenant_id == tenant_id,
            DataExport.status.in_((ExportStatus.QUEUED, ExportStatus.RUNNING)),
        )
    )
    if running is not None:
        raise Conflict("An export is already being prepared.", code="export_in_progress")
    export = DataExport(tenant_id=tenant_id, requested_by_user_id=user_id, status=ExportStatus.QUEUED)
    session.add(export)
    await session.flush()
    return export


async def _write_table(
    session: AsyncSession, archive: zipfile.ZipFile, table: Table, column: str, tenant_id: uuid.UUID
) -> int:
    columns = [c for c in table.columns if c.name not in EXPORT_SKIP_COLUMNS]
    order = [c for c in table.primary_key.columns]
    count = 0
    offset = 0
    with archive.open(f"{table.name}.jsonl", "w", force_zip64=True) as out:
        while True:
            rows = (
                (
                    await session.execute(
                        select(*columns)
                        .where(table.c[column] == tenant_id)
                        .order_by(*order)
                        .limit(EXPORT_BATCH)
                        .offset(offset)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                out.write(json.dumps(dict(row), default=_jsonable, ensure_ascii=False).encode() + b"\n")
            count += len(rows)
            if len(rows) < EXPORT_BATCH:
                return count
            offset += EXPORT_BATCH


async def _write_members(session: AsyncSession, archive: zipfile.ZipFile, tenant_id: uuid.UUID) -> int:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT u.id AS user_id, u.full_name, u.email::text AS email, m.role, m.status, "
                    "m.created_at AS joined_at FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
                    "WHERE m.tenant_id = :t ORDER BY m.created_at"
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .all()
    )
    with archive.open("team_members.jsonl", "w") as out:
        for row in rows:
            out.write(json.dumps(dict(row), default=_jsonable, ensure_ascii=False).encode() + b"\n")
    return len(rows)


async def build_export(export_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """Worker: write every table of the business to JSON Lines inside a zip, store it
    privately and email the person who asked. Safe to run again for the same export."""
    async with app_transaction(tenant_id) as session:
        export = await session.get(DataExport, export_id, with_for_update=True)
        if export is None or export.status not in (ExportStatus.QUEUED, ExportStatus.RUNNING):
            return "skipped"
        export.status = ExportStatus.RUNNING
    settings = get_settings()
    key = f"tenants/{tenant_id}/exports/{export_id}.zip"
    counts: dict[str, int] = {}
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        tables = {name: col for name, col in tenant_tables().items() if name not in EXPORT_SKIP_TABLES}
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            # One read-only snapshot, so related tables agree with each other.
            async with get_app_sessionmaker()() as session, session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                await set_tenant_context(session, tenant_id)
                for name, column in tables.items():
                    counts[name] = await _write_table(
                        session, archive, Base.metadata.tables[name], column, tenant_id
                    )
                counts["team_members"] = await _write_members(session, archive, tenant_id)
            archive.writestr(
                "README.txt",
                "XploWhatsApp data export\n\n"
                f"Business: {tenant_id}\nCreated: {utcnow().isoformat()}\n\n"
                "Each .jsonl file holds one table, one JSON object per line. Secrets (API keys, "
                "WhatsApp access tokens) and search indexes are not included.\n\n"
                + "\n".join(f"{n}: {c} rows" for n, c in counts.items())
                + "\n",
            )
        size = await asyncio.to_thread(os.path.getsize, path)
        await s3.put_file(key, path, "application/zip")
    except Exception as exc:
        log.exception("data_export_failed", export_id=str(export_id))
        async with app_transaction(tenant_id) as session:
            export = await session.get(DataExport, export_id)
            if export is not None:
                export.status = ExportStatus.FAILED
                export.error = f"The export could not be completed ({type(exc).__name__})."[:300]
        return "failed"
    finally:
        os.unlink(path)

    async with app_transaction(tenant_id) as session:
        export = await session.get(DataExport, export_id)
        assert export is not None
        export.status = ExportStatus.READY
        export.object_key = key
        export.size_bytes = size
        export.completed_at = utcnow()
        export.expires_at = utcnow() + timedelta(days=settings.data_export_ttl_days)
        await audit.record(
            session,
            "privacy.export_ready",
            tenant_id=tenant_id,
            actor_user_id=None,
            target_type="data_export",
            target_id=export_id,
            details={"size_bytes": size, "rows": sum(counts.values())},
        )
        if export.requested_by_user_id:
            await _email_export_ready(session, export.requested_by_user_id, export.expires_at)
    return "ready"


async def _email_export_ready(session: AsyncSession, user_id: uuid.UUID, expires: datetime) -> None:
    from app.modules.auth.models import User
    from app.modules.notifications.email import OutgoingEmail, send_email

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return
    link = f"{get_settings().frontend_base_url.rstrip('/')}/settings/privacy"
    email = OutgoingEmail(
        to=user.email,
        subject="Your XploWhatsApp data export is ready",
        text=(
            "The export of your business data is ready to download.\n\n"
            f"Download it from Privacy and data: {link}\n\n"
            f"It will be deleted on {expires:%d %B %Y}. The file holds your customers' personal "
            "data: keep it somewhere safe."
        ),
    )

    async def _send() -> None:
        send_email(email)

    run_after_commit(session, _send)


# ---------------------------------------------------------------------- business deletion


async def schedule_business_deletion(
    session: AsyncSession, tenant: Tenant, actor_id: uuid.UUID, meta: RequestMeta
) -> datetime:
    sub = (
        await session.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
    ).scalar_one_or_none()
    if (
        sub is not None
        and sub.gateway_subscription_id
        and sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE)
        and not sub.cancel_at_period_end
    ):
        raise Conflict(
            "Cancel the paid plan on the Billing page first, so you aren't charged again.",
            code="cancel_plan_first",
        )
    now = utcnow()
    purge_after = now + timedelta(days=get_settings().business_deletion_grace_days)
    tenant.deleted_at = now
    tenant.purge_after = purge_after
    await audit.record(
        session,
        "tenant.deletion_requested",
        tenant_id=tenant.id,
        actor_user_id=actor_id,
        meta=meta,
        target_type="tenant",
        target_id=tenant.id,
        details={"purge_after": purge_after.isoformat()},
    )
    return purge_after


# Kept after a purge: the business row (renamed), its subscription, tax invoices and
# payments (tax law requires them for years, and they hold only the billing snapshot),
# gateway events behind those payments, AI usage costs, and the audit log.
PURGE_KEEP_TABLES = {
    "tenants",
    "subscriptions",
    "billing_invoices",
    "billing_transactions",
    "billing_events",
    "usage_records",
    "audit_logs",
}


async def purge_business(tenant_id: uuid.UUID) -> dict[str, int]:
    """Remove a deleted business's operational data once its grace period is over.

    Runs on the admin role: it must remove rows the application role may never delete
    (WhatsApp credentials, append-only histories). Every statement names the business.
    """
    removed: dict[str, int] = {}
    tables = [(n, c) for n, c in tenant_tables().items() if n not in PURGE_KEEP_TABLES]
    async with admin_transaction() as session:
        tenant = await session.get(Tenant, tenant_id, with_for_update=True)
        if tenant is None or tenant.deleted_at is None or tenant.purged_at is not None:
            return removed
        if tenant.purge_after is None or tenant.purge_after > utcnow():
            return removed
        for name, column in reversed(tables):
            table = Base.metadata.tables[name]
            result = await session.execute(delete(table).where(table.c[column] == tenant_id))
            if result.rowcount:  # type: ignore[attr-defined]
                removed[name] = int(result.rowcount)  # type: ignore[attr-defined]
        await session.execute(
            text("UPDATE refresh_tokens SET tenant_id = NULL WHERE tenant_id = :t"), {"t": tenant_id}
        )
        tenant.name = "Deleted business"
        tenant.slug = f"deleted-{tenant_id.hex[:16]}"
        tenant.settings = {}
        tenant.industry = None
        tenant.purged_at = utcnow()
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor_user_id=None,
                action="tenant.purged",
                target_type="tenant",
                target_id=str(tenant_id),
                details={"rows": removed},
            )
        )
    try:
        removed["files"] = await s3.delete_prefix(f"tenants/{tenant_id}/")
    except s3.StorageUnavailable:
        log.warning("purge_files_failed", tenant_id=str(tenant_id))
    return removed


async def restore_business(session: AsyncSession, tenant: Tenant) -> None:
    if tenant.deleted_at is None:
        raise Conflict("This business isn't deleted.", code="not_deleted")
    if tenant.purged_at is not None:
        raise Conflict("This business's data has already been purged.", code="already_purged")
    tenant.deleted_at = None
    tenant.purge_after = None


# ---------------------------------------------------------------------- retention


async def apply_message_retention(tenant_id: uuid.UUID, days: int, batch: int = 5000) -> int:
    """Delete this business's messages older than its retention limit, in batches."""
    cutoff = utcnow() - timedelta(days=days)
    total = 0
    while True:
        async with app_transaction(tenant_id) as session:
            ids = (
                select(Message.id)
                .where(Message.tenant_id == tenant_id, Message.created_at < cutoff)
                .limit(batch)
            )
            result = await session.execute(
                delete(Message).where(Message.tenant_id == tenant_id, Message.id.in_(ids))
            )
            n = int(result.rowcount or 0)  # type: ignore[attr-defined]
        total += n
        if n < batch:
            return total
