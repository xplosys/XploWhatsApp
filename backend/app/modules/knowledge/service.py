"""Knowledge documents: adding, versioning, processing, re-indexing and deleting.

Processing a version runs in three steps so no database transaction is open while the
file is fetched, scanned, read or embedded:

1. claim the version (status `processing`) and resolve the embedding models and keys;
2. fetch or download, scan, extract, chunk and embed, with no transaction;
3. store chunks and vectors, make the version active, and drop the previous version's
   chunks, in one transaction. If anything changed meanwhile, the work is retried.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.session import app_transaction, run_after_commit
from app.modules.ai import catalogue
from app.modules.ai.catalogue import CallRecord
from app.modules.ai.models import AIModel, ModelKind
from app.modules.billing.service import effective_plan
from app.modules.knowledge import embeddings, extract, webfetch
from app.modules.knowledge.chunking import Chunk, chunk_sections
from app.modules.knowledge.filetypes import MIME_TYPES
from app.modules.knowledge.models import (
    FileType,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeEmbedding,
    KnowledgeSettings,
    SourceType,
    VersionStatus,
)
from app.modules.storage import clamav, s3
from app.workers.dispatch import KNOWLEDGE_PROCESS, KNOWLEDGE_REINDEX, dispatch

log = structlog.get_logger(__name__)

STALE_PROCESSING = timedelta(minutes=15)
DEFAULT_MIN_SCORE = Decimal("0.300")
REINDEX_BATCH = 500


class QuotaExceeded(Conflict):
    code = "plan_limit_reached"


# ---- settings -----------------------------------------------------------------------


async def get_knowledge_settings(session: AsyncSession, tenant_id: uuid.UUID) -> KnowledgeSettings | None:
    return (
        await session.execute(select(KnowledgeSettings).where(KnowledgeSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def ensure_settings(
    session: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False
) -> KnowledgeSettings:
    stmt = select(KnowledgeSettings).where(KnowledgeSettings.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        await session.execute(
            insert(KnowledgeSettings)
            .values(id=uuid.uuid4(), tenant_id=tenant_id)
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        row = (await session.execute(stmt)).scalar_one()
    return row


async def embedding_model(session: AsyncSession, provider: str, model_id: str) -> AIModel:
    model = await catalogue.find_model(session, provider, model_id)
    if model is None or not model.active or model.kind != ModelKind.EMBEDDING:
        raise ValidationFailed(f"{model_id} is not an available embedding model.", code="model_unavailable")
    return model


async def change_embedding_model(
    session: AsyncSession, settings_row: KnowledgeSettings, model: AIModel, user_id: uuid.UUID
) -> bool:
    """Returns True when existing chunks must be re-indexed for the new model."""
    settings_row.updated_by_user_id = user_id
    if settings_row.embedding_model_pk == model.id:
        settings_row.pending_embedding_model_pk = None
        settings_row.reindex_error = None
        return False
    has_chunks = await session.scalar(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(KnowledgeChunk.tenant_id == settings_row.tenant_id)
    )
    if settings_row.embedding_model_pk is None or not has_chunks:
        settings_row.embedding_model_pk = model.id
        settings_row.pending_embedding_model_pk = None
        settings_row.reindex_error = None
        return False
    settings_row.pending_embedding_model_pk = model.id
    settings_row.reindex_error = None
    tenant_id = settings_row.tenant_id
    run_after_commit(session, lambda: dispatch(KNOWLEDGE_REINDEX, tenant_id))
    return True


# ---- quotas -------------------------------------------------------------------------


async def usage(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[int, int]:
    documents = await session.scalar(
        select(func.count()).select_from(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant_id)
    )
    stored = await session.scalar(
        select(func.coalesce(func.sum(KnowledgeDocumentVersion.size_bytes), 0)).where(
            KnowledgeDocumentVersion.tenant_id == tenant_id, KnowledgeDocumentVersion.object_key.is_not(None)
        )
    )
    return int(documents or 0), int(stored or 0)


async def limits(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[int, int]:
    plan = await effective_plan(session, tenant_id)
    return plan.limit("max_knowledge_documents", 0), plan.limit("max_storage_mb", 0) * 1024 * 1024


async def check_quota(
    session: AsyncSession, tenant_id: uuid.UUID, *, new_document: bool, add_bytes: int
) -> None:
    documents, stored = await usage(session, tenant_id)
    max_documents, max_bytes = await limits(session, tenant_id)
    if new_document and documents >= max_documents:
        raise QuotaExceeded(f"Your plan allows {max_documents} knowledge documents. Delete one or upgrade.")
    if stored + add_bytes > max_bytes:
        raise QuotaExceeded(
            f"Your plan includes {max_bytes // (1024 * 1024)} MB of knowledge storage. "
            "Delete old documents or versions, or upgrade."
        )


# ---- adding documents and versions --------------------------------------------------


def _object_key(tenant_id: uuid.UUID, document_id: uuid.UUID, label: str) -> str:
    """Random, tenant-prefixed keys; the user's file name is never part of a key."""
    return f"tenants/{tenant_id}/knowledge/{document_id}/{label}-{secrets.token_hex(8)}"


async def _require_embedding_model(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    row = await get_knowledge_settings(session, tenant_id)
    if row is None or row.embedding_model_pk is None:
        raise Conflict(
            "Choose an embedding model in Knowledge settings before adding documents.",
            code="embedding_model_required",
        )


async def get_document(
    session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
) -> KnowledgeDocument:
    stmt = select(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.id == document_id
    )
    if lock:
        stmt = stmt.with_for_update()
    doc = (await session.execute(stmt)).scalar_one_or_none()
    if doc is None:
        raise NotFound("Document not found.", code="document_not_found")
    return doc


async def _new_version(
    session: AsyncSession,
    doc: KnowledgeDocument,
    user_id: uuid.UUID | None,
    *,
    data: bytes | None = None,
    file_type: FileType | None = None,
    filename: str | None = None,
) -> KnowledgeDocumentVersion:
    now = datetime.now(UTC)
    doc.latest_version += 1
    version = KnowledgeDocumentVersion(
        id=uuid.uuid4(),
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        version=doc.latest_version,
        status=VersionStatus.QUEUED,
        original_filename=filename,
        file_type=file_type,
        created_by_user_id=user_id,
        created_at=now,
        updated_at=now,
    )
    if data is not None and file_type is not None:
        # Stored before the row is committed; if the commit fails the object is orphaned
        # (harmless: random key, private bucket) rather than a row pointing at nothing.
        key = _object_key(doc.tenant_id, doc.id, f"v{version.version}")
        await s3.put(key, data, MIME_TYPES[file_type])
        version.object_key = key
        version.size_bytes = len(data)
        version.sha256 = hashlib.sha256(data).hexdigest()
    session.add(version)
    await session.flush()
    tenant_id, version_id = doc.tenant_id, version.id
    run_after_commit(session, lambda: dispatch(KNOWLEDGE_PROCESS, version_id, tenant_id))
    return version


async def add_file(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    title: str,
    filename: str,
    file_type: FileType,
    data: bytes,
    document: KnowledgeDocument | None = None,
) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion]:
    await _require_embedding_model(session, tenant_id)
    await check_quota(session, tenant_id, new_document=document is None, add_bytes=len(data))
    if document is not None and document.source_type != SourceType.FILE:
        raise ValidationFailed("Only uploaded files can be replaced with a new file.", code="wrong_source")
    if document is not None and document.active_version is not None:
        current = await version_by_number(session, document, document.active_version)
        if current is not None and current.sha256 == hashlib.sha256(data).hexdigest():
            raise Conflict("This file is identical to the current version.", code="unchanged")
    if document is None:
        document = KnowledgeDocument(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            title=title,
            source_type=SourceType.FILE,
            latest_version=0,
            created_by_user_id=user_id,
        )
        session.add(document)
        await session.flush()
    version = await _new_version(
        session, document, user_id, data=data, file_type=file_type, filename=filename
    )
    return document, version


async def add_text(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    title: str,
    text: str,
    document: KnowledgeDocument | None = None,
) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion]:
    data = text.encode("utf-8")
    await _require_embedding_model(session, tenant_id)
    await check_quota(session, tenant_id, new_document=document is None, add_bytes=len(data))
    if document is None:
        document = KnowledgeDocument(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            title=title,
            source_type=SourceType.TEXT,
            latest_version=0,
            created_by_user_id=user_id,
        )
        session.add(document)
        await session.flush()
    elif document.source_type != SourceType.TEXT:
        raise ValidationFailed("Only notes can be edited as text.", code="wrong_source")
    else:
        document.title = title
    version = await _new_version(
        session, document, user_id, data=data, file_type=FileType.MARKDOWN, filename=None
    )
    return document, version


async def add_url(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, *, title: str | None, url: str
) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion]:
    try:
        url = webfetch.validate_url(url)
    except webfetch.FetchError as exc:
        raise ValidationFailed(str(exc), code=exc.code) from exc
    await _require_embedding_model(session, tenant_id)
    await check_quota(session, tenant_id, new_document=True, add_bytes=0)
    document = KnowledgeDocument(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title=(title or urlsplit(url).hostname or url)[:200],
        source_type=SourceType.URL,
        source_url=url,
        latest_version=0,
        created_by_user_id=user_id,
    )
    session.add(document)
    await session.flush()
    return document, await _new_version(session, document, user_id)


async def refresh_url(
    session: AsyncSession, document: KnowledgeDocument, user_id: uuid.UUID
) -> KnowledgeDocumentVersion:
    if document.source_type != SourceType.URL:
        raise ValidationFailed("Only web pages can be fetched again.", code="wrong_source")
    await _require_embedding_model(session, document.tenant_id)
    return await _new_version(session, document, user_id)


async def version_by_number(
    session: AsyncSession, document: KnowledgeDocument, number: int
) -> KnowledgeDocumentVersion | None:
    return (
        await session.execute(
            select(KnowledgeDocumentVersion).where(
                KnowledgeDocumentVersion.document_id == document.id,
                KnowledgeDocumentVersion.version == number,
            )
        )
    ).scalar_one_or_none()


async def versions(session: AsyncSession, document: KnowledgeDocument) -> list[KnowledgeDocumentVersion]:
    return list(
        (
            await session.execute(
                select(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.document_id == document.id)
                .order_by(KnowledgeDocumentVersion.version.desc())
            )
        ).scalars()
    )


async def retry(session: AsyncSession, document: KnowledgeDocument, number: int) -> KnowledgeDocumentVersion:
    version = await version_by_number(session, document, number)
    if version is None:
        raise NotFound("Version not found.")
    stale = (
        version.status == VersionStatus.PROCESSING
        and version.updated_at < datetime.now(UTC) - STALE_PROCESSING
    )
    if version.status not in (VersionStatus.FAILED, VersionStatus.QUEUED) and not stale:
        raise Conflict("Only a failed or stuck version can be processed again.", code="not_retryable")
    if document.active_version and document.active_version > version.version:
        raise Conflict("A newer version is already active.", code="superseded")
    version.status = VersionStatus.QUEUED
    version.error = version.error_code = None
    version.updated_at = datetime.now(UTC)
    tenant_id, version_id = document.tenant_id, version.id
    run_after_commit(session, lambda: dispatch(KNOWLEDGE_PROCESS, version_id, tenant_id))
    return version


async def delete_document(session: AsyncSession, document: KnowledgeDocument) -> None:
    keys = [v.object_key for v in await versions(session, document) if v.object_key]
    await session.delete(document)
    await session.flush()

    async def remove_objects() -> None:
        for key in keys:
            try:
                await s3.delete(key)
            except s3.StorageUnavailable:
                log.warning("knowledge_object_delete_failed", key=key)

    run_after_commit(session, remove_objects)


async def delete_version(session: AsyncSession, document: KnowledgeDocument, number: int) -> None:
    """Delete an old version's stored file to free space. The active version stays."""
    version = await version_by_number(session, document, number)
    if version is None:
        raise NotFound("Version not found.")
    if version.version == document.active_version:
        raise Conflict(
            "The active version can't be deleted. Delete the document instead.", code="active_version"
        )
    if version.status in (VersionStatus.QUEUED, VersionStatus.PROCESSING):
        raise Conflict("Wait until this version has finished processing.", code="processing")
    key = version.object_key
    await session.delete(version)
    await session.flush()
    if key:

        async def remove_object() -> None:
            try:
                await s3.delete(key)
            except s3.StorageUnavailable:
                log.warning("knowledge_object_delete_failed", key=key)

        run_after_commit(session, remove_object)


# ---- processing ---------------------------------------------------------------------


@dataclass
class _Claim:
    version_id: uuid.UUID
    attempts: int
    document_id: uuid.UUID
    source_type: str
    source_url: str | None
    object_key: str | None
    file_type: str | None
    size_bytes: int
    embedders: list[embeddings.Embedder]


def _fail(
    version: KnowledgeDocumentVersion, code: str, message: str, status: str = VersionStatus.FAILED
) -> None:
    version.status = status
    version.error_code = code[:64]
    version.error = message[:500]
    version.updated_at = datetime.now(UTC)


async def _claim(tenant_id: uuid.UUID, version_id: uuid.UUID) -> _Claim | str:
    async with app_transaction(tenant_id) as session:
        version = await session.get(KnowledgeDocumentVersion, version_id, with_for_update=True)
        if version is None:
            return "missing"
        now = datetime.now(UTC)
        if version.status == VersionStatus.PROCESSING and version.updated_at > now - STALE_PROCESSING:
            return "busy"
        if version.status not in (VersionStatus.QUEUED, VersionStatus.PROCESSING):
            return version.status
        document = await session.get(KnowledgeDocument, version.document_id)
        assert document is not None
        if document.active_version and document.active_version > version.version:
            version.status = VersionStatus.REPLACED
            version.updated_at = now
            return "superseded"
        settings_row = await get_knowledge_settings(session, tenant_id)
        model_pks = [
            pk
            for pk in (
                settings_row.embedding_model_pk if settings_row else None,
                settings_row.pending_embedding_model_pk if settings_row else None,
            )
            if pk
        ]
        if not model_pks:
            _fail(version, "embedding_model_required", "Choose an embedding model in Knowledge settings.")
            return "failed"
        try:
            embedders = [await embeddings.embedder_for(session, tenant_id, pk) for pk in model_pks]
        except embeddings.EmbedderUnavailable as exc:
            _fail(version, exc.code, str(exc))
            return "failed"
        version.status = VersionStatus.PROCESSING
        version.attempts += 1
        version.updated_at = now
        return _Claim(
            version.id,
            version.attempts,
            document.id,
            document.source_type,
            document.source_url,
            version.object_key,
            version.file_type,
            version.size_bytes,
            embedders,
        )


async def _finish_failed(
    tenant_id: uuid.UUID,
    claim: _Claim,
    code: str,
    message: str,
    *,
    status: str = VersionStatus.FAILED,
    calls: list[CallRecord] | None = None,
    scan_result: str | None = None,
) -> str:
    async with app_transaction(tenant_id) as session:
        for call in calls or []:
            await catalogue.record_call(session, tenant_id, call)
        version = await session.get(KnowledgeDocumentVersion, claim.version_id, with_for_update=True)
        if version is None or version.attempts != claim.attempts:
            return "superseded"
        _fail(version, code, message, status)
        if scan_result:
            version.scan_result = scan_result
        if status == VersionStatus.REJECTED:
            version.object_key = None
            version.size_bytes = 0
    return status


@dataclass
class _Work:
    data: bytes
    file_type: str
    object_key: str | None
    new_object: bool
    scan_result: str
    title: str | None
    chunks: list[Chunk]
    char_count: int
    vectors: dict[uuid.UUID, list[list[float]]]
    calls: list[CallRecord]


async def process_version(version_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    claim = await _claim(tenant_id, version_id)
    if isinstance(claim, str):
        return claim
    settings = get_settings()
    max_bytes = settings.knowledge_max_file_mb * 1024 * 1024
    new_object = False
    charset: str | None = None
    # 1. Get the bytes.
    try:
        if claim.source_type == SourceType.URL:
            assert claim.source_url
            fetched = await webfetch.fetch(claim.source_url)
            data, file_type, charset = fetched.body, fetched.file_type.value, fetched.charset
            object_key = _object_key(tenant_id, claim.document_id, "web")
        else:
            assert claim.object_key and claim.file_type
            data = await s3.get(claim.object_key, max_bytes)
            file_type, object_key = claim.file_type, claim.object_key
    except webfetch.FetchError as exc:
        return await _finish_failed(tenant_id, claim, exc.code, str(exc))
    except s3.StorageUnavailable as exc:
        return await _finish_failed(tenant_id, claim, "storage_unavailable", exc.message)
    # 2. Scan before anything parses it.
    if clamav.scanner_configured():
        try:
            verdict = await clamav.scan(data)
        except clamav.ScannerUnavailable as exc:
            return await _finish_failed(tenant_id, claim, "scanner_unavailable", str(exc))
        if not verdict.clean:
            if claim.object_key:
                try:
                    await s3.delete(claim.object_key)
                except s3.StorageUnavailable:
                    log.warning("infected_object_delete_failed", key=claim.object_key)
            log.warning("knowledge_file_infected", version_id=str(version_id), signature=verdict.signature)
            return await _finish_failed(
                tenant_id,
                claim,
                "malware_found",
                f"The virus scanner found {verdict.signature}. The file was deleted.",
                status=VersionStatus.REJECTED,
                scan_result=verdict.summary,
            )
        scan_result = "clean"
    else:
        scan_result = "not scanned (no scanner configured in development)"
    # 3. Store a fetched page so the version can be downloaded and re-read later.
    if claim.source_type == SourceType.URL:
        try:
            await s3.put(object_key, data, MIME_TYPES[FileType(file_type)])
            new_object = True
        except s3.StorageUnavailable as exc:
            return await _finish_failed(tenant_id, claim, "storage_unavailable", exc.message)
    # 4. Read and split.
    try:
        extracted = extract.extract(file_type, data, charset)
    except extract.ExtractionError as exc:
        return await _finish_failed(tenant_id, claim, exc.code, str(exc))
    chunks = chunk_sections(
        extracted.sections,
        chunk_tokens=settings.knowledge_chunk_tokens,
        overlap_tokens=settings.knowledge_chunk_overlap_tokens,
    )
    if len(chunks) > settings.knowledge_max_chunks_per_document:
        return await _finish_failed(
            tenant_id, claim, "too_large", "The document is too long. Split it into smaller documents."
        )
    # 5. Embed for every model in use (the active one and one being switched to).
    vectors: dict[uuid.UUID, list[list[float]]] = {}
    calls: list[CallRecord] = []
    for embedder in claim.embedders:
        try:
            outcome = await embeddings.embed(
                embedder, [c.content for c in chunks], task="document", purpose="knowledge_index"
            )
        except embeddings.EmbedFailed as exc:
            return await _finish_failed(
                tenant_id, claim, exc.code, f"The embedding provider failed: {exc}", calls=[*calls, exc.call]
            )
        vectors[embedder.model_pk] = outcome.vectors
        calls.append(outcome.call)
    work = _Work(
        data,
        file_type,
        object_key,
        new_object,
        scan_result,
        extracted.title,
        chunks,
        extracted.char_count,
        vectors,
        calls,
    )
    return await _store(tenant_id, claim, work)


async def _store(tenant_id: uuid.UUID, claim: _Claim, work: _Work) -> str:
    async with app_transaction(tenant_id) as session:
        for call in work.calls:
            await catalogue.record_call(session, tenant_id, call)
        version = await session.get(KnowledgeDocumentVersion, claim.version_id, with_for_update=True)
        if (
            version is None
            or version.attempts != claim.attempts
            or version.status != VersionStatus.PROCESSING
        ):
            return "superseded"
        document = await session.get(KnowledgeDocument, claim.document_id, with_for_update=True)
        assert document is not None
        settings_row = await get_knowledge_settings(session, tenant_id)
        wanted = (
            {pk for pk in (settings_row.embedding_model_pk, settings_row.pending_embedding_model_pk) if pk}
            if settings_row
            else set()
        )
        if not wanted <= set(work.vectors):
            # The embedding model changed while this ran: do it again with the new one.
            version.status = VersionStatus.QUEUED
            version.updated_at = datetime.now(UTC)
            run_after_commit(session, lambda: dispatch(KNOWLEDGE_PROCESS, claim.version_id, tenant_id))
            return "requeued"
        if document.active_version and document.active_version > version.version:
            version.status = VersionStatus.REPLACED
            return "superseded"
        if work.new_object:
            documents_bytes = (await usage(session, tenant_id))[1]
            max_bytes = (await limits(session, tenant_id))[1]
            if documents_bytes + len(work.data) > max_bytes:
                _fail(version, "plan_limit_reached", "Your plan's knowledge storage is full.")
                key = work.object_key
                if key:
                    run_after_commit(session, lambda: s3.delete(key))
                return "failed"
            version.object_key = work.object_key
            version.size_bytes = len(work.data)
            version.sha256 = hashlib.sha256(work.data).hexdigest()
            version.file_type = work.file_type
        rows: list[dict[str, Any]] = []
        for ordinal, chunk in enumerate(work.chunks):
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "document_id": document.id,
                    "version_id": version.id,
                    "ordinal": ordinal,
                    "heading": chunk.heading,
                    "content": chunk.content,
                    "token_estimate": chunk.token_estimate,
                    "meta": chunk.meta,
                }
            )
        # A retried run may have left nothing behind (one transaction), but be safe.
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.version_id == version.id))
        if rows:
            await session.execute(insert(KnowledgeChunk), rows)
            for model_pk, vectors in work.vectors.items():
                if model_pk not in wanted:
                    continue  # a model that was switched away from while this ran
                await session.execute(
                    insert(KnowledgeEmbedding),
                    [
                        {
                            "id": uuid.uuid4(),
                            "tenant_id": tenant_id,
                            "chunk_id": row["id"],
                            "model_pk": model_pk,
                            "embedding": vector,
                        }
                        for row, vector in zip(rows, vectors, strict=True)
                    ],
                )
        previous = document.active_version
        if previous and previous != version.version:
            old = await version_by_number(session, document, previous)
            if old is not None:
                await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.version_id == old.id))
                old.status = VersionStatus.REPLACED
                old.chunk_count = 0
                old.updated_at = datetime.now(UTC)
        document.active_version = version.version
        if (
            document.source_type == SourceType.URL
            and work.title
            and document.title == urlsplit(document.source_url or "").hostname
        ):
            document.title = work.title[:200]
        now = datetime.now(UTC)
        version.status = VersionStatus.READY
        version.error = version.error_code = None
        version.scan_result = work.scan_result
        version.char_count = work.char_count
        version.chunk_count = len(rows)
        version.processed_at = now
        version.updated_at = now
    return "ready"


# ---- re-indexing after an embedding model change ------------------------------------


async def reindex(tenant_id: uuid.UUID) -> str:
    """Add vectors for the pending model to every chunk, then switch to it. Runs in
    batches; answers keep using the current model until the switch."""
    while True:
        async with app_transaction(tenant_id) as session:
            settings_row = await ensure_settings(session, tenant_id, lock=True)
            pending = settings_row.pending_embedding_model_pk
            if pending is None:
                return "nothing_to_do"
            try:
                embedder = await embeddings.embedder_for(session, tenant_id, pending)
            except embeddings.EmbedderUnavailable as exc:
                settings_row.reindex_error = str(exc)[:500]
                return "failed"
            done = select(KnowledgeEmbedding.chunk_id).where(KnowledgeEmbedding.model_pk == pending)
            todo = list(
                (
                    await session.execute(
                        select(KnowledgeChunk.id, KnowledgeChunk.content)
                        .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.id.not_in(done))
                        .order_by(KnowledgeChunk.id)
                        .limit(REINDEX_BATCH)
                    )
                ).all()
            )
            if not todo:
                old = settings_row.embedding_model_pk
                settings_row.embedding_model_pk = pending
                settings_row.pending_embedding_model_pk = None
                settings_row.reindex_error = None
                if old and old != pending:
                    await session.execute(
                        delete(KnowledgeEmbedding).where(
                            KnowledgeEmbedding.tenant_id == tenant_id, KnowledgeEmbedding.model_pk == old
                        )
                    )
                return "switched"
        try:
            outcome = await embeddings.embed(
                embedder, [row.content for row in todo], task="document", purpose="knowledge_reindex"
            )
        except embeddings.EmbedFailed as exc:
            async with app_transaction(tenant_id) as session:
                await catalogue.record_call(session, tenant_id, exc.call)
                settings_row = await ensure_settings(session, tenant_id, lock=True)
                settings_row.reindex_error = f"The embedding provider failed: {exc}"[:500]
            return "failed"
        async with app_transaction(tenant_id) as session:
            await catalogue.record_call(session, tenant_id, outcome.call)
            # Chunks of a document replaced or deleted meanwhile are gone; skip them.
            alive = set(
                (
                    await session.execute(
                        select(KnowledgeChunk.id).where(KnowledgeChunk.id.in_([row.id for row in todo]))
                    )
                ).scalars()
            )
            values = [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "chunk_id": row.id,
                    "model_pk": pending,
                    "embedding": vector,
                }
                for row, vector in zip(todo, outcome.vectors, strict=True)
                if row.id in alive
            ]
            if values:
                await session.execute(
                    insert(KnowledgeEmbedding)
                    .values(values)
                    .on_conflict_do_nothing(index_elements=["chunk_id", "model_pk"])
                )
