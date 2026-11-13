"""Knowledge base API: settings, documents (files, web pages, notes), versions and a
search test that shows what the assistant would find for a question."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from sqlalchemy import func, select

from app.api.deps import Meta, Requires, TenantContext
from app.core.config import get_settings
from app.core.errors import NotFound, ValidationFailed
from app.core.rate_limit import hit
from app.modules.ai import catalogue
from app.modules.ai.models import AIModel, ModelKind
from app.modules.audit.service import record
from app.modules.knowledge import retrieval, service
from app.modules.knowledge.filetypes import UnsupportedFile, detect
from app.modules.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeEmbedding,
    VersionStatus,
)
from app.modules.knowledge.schemas import (
    ChunkOut,
    DocumentDetail,
    DocumentOut,
    DownloadOut,
    EmbeddingModelOut,
    KnowledgeUsageOut,
    PassageOut,
    SearchIn,
    SearchOut,
    SettingsOut,
    SettingsUpdate,
    TextIn,
    UrlIn,
    VersionOut,
)
from app.modules.rbac.permissions import Permission
from app.modules.storage import clamav, s3

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

Reader = Annotated[TenantContext, Requires(Permission.KNOWLEDGE_READ)]
Manager = Annotated[TenantContext, Requires(Permission.KNOWLEDGE_MANAGE)]

UPLOADS_PER_MINUTE = 20
SEARCHES_PER_MINUTE = 20


def _model_out(model: AIModel | None) -> EmbeddingModelOut | None:
    if model is None:
        return None
    return EmbeddingModelOut(
        id=model.id, provider=model.provider, model_id=model.model_id, display_name=model.display_name
    )


async def _settings_out(ctx: TenantContext) -> SettingsOut:
    row = await service.get_knowledge_settings(ctx.session, ctx.tenant_id)
    documents, stored = await service.usage(ctx.session, ctx.tenant_id)
    max_documents, max_bytes = await service.limits(ctx.session, ctx.tenant_id)
    active = (
        await ctx.session.get(AIModel, row.embedding_model_pk) if row and row.embedding_model_pk else None
    )
    pending = (
        await ctx.session.get(AIModel, row.pending_embedding_model_pk)
        if row and row.pending_embedding_model_pk
        else None
    )
    done = total = 0
    if pending is not None:
        total = int(
            await ctx.session.scalar(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.tenant_id == ctx.tenant_id)
            )
            or 0
        )
        done = int(
            await ctx.session.scalar(
                select(func.count())
                .select_from(KnowledgeEmbedding)
                .where(
                    KnowledgeEmbedding.tenant_id == ctx.tenant_id, KnowledgeEmbedding.model_pk == pending.id
                )
            )
            or 0
        )
    settings = get_settings()
    return SettingsOut(
        embedding_model=_model_out(active),
        pending_embedding_model=_model_out(pending),
        reindex_error=row.reindex_error if row else None,
        reindex_done=done,
        reindex_total=total,
        min_score=row.min_score if row else service.DEFAULT_MIN_SCORE,
        top_k=row.top_k if row else 5,
        usage=KnowledgeUsageOut(
            documents=documents,
            max_documents=max_documents,
            storage_bytes=stored,
            max_storage_bytes=max_bytes,
        ),
        storage_ready=s3.storage_configured(),
        virus_scan=clamav.scanner_configured(),
        max_file_mb=settings.knowledge_max_file_mb,
    )


@router.get("/settings", response_model=SettingsOut)
async def get_knowledge_settings(ctx: Reader) -> SettingsOut:
    return await _settings_out(ctx)


@router.put("/settings", response_model=SettingsOut)
async def update_knowledge_settings(body: SettingsUpdate, ctx: Manager, meta: Meta) -> SettingsOut:
    row = await service.ensure_settings(ctx.session, ctx.tenant_id, lock=True)
    details: dict[str, Any] = {}
    if body.embedding_model is not None or body.embedding_provider is not None:
        if not (body.embedding_model and body.embedding_provider):
            raise ValidationFailed("Give both the provider and the model.", code="model_incomplete")
        model = await service.embedding_model(ctx.session, body.embedding_provider, body.embedding_model)
        reindex = await service.change_embedding_model(ctx.session, row, model, ctx.user.id)
        details.update(embedding_model=f"{model.provider}/{model.model_id}", reindex=reindex)
    if body.min_score is not None:
        row.min_score = body.min_score
        details["min_score"] = str(body.min_score)
    if body.top_k is not None:
        row.top_k = body.top_k
        details["top_k"] = body.top_k
    row.updated_by_user_id = ctx.user.id
    await ctx.session.flush()
    await record(
        ctx.session,
        "knowledge.settings_updated",
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="knowledge_settings",
        target_id=row.id,
        details=details,
    )
    return await _settings_out(ctx)


@router.post("/settings/reindex", response_model=SettingsOut)
async def retry_reindex(ctx: Manager) -> SettingsOut:
    row = await service.ensure_settings(ctx.session, ctx.tenant_id, lock=True)
    if row.pending_embedding_model_pk is None:
        raise ValidationFailed("No embedding model change is in progress.", code="nothing_to_reindex")
    model = await ctx.session.get(AIModel, row.pending_embedding_model_pk)
    assert model is not None
    row.reindex_error = None
    row.pending_embedding_model_pk = None
    await service.change_embedding_model(ctx.session, row, model, ctx.user.id)
    await ctx.session.flush()
    return await _settings_out(ctx)


@router.get("/embedding-models", response_model=list[EmbeddingModelOut])
async def list_embedding_models(ctx: Reader) -> list[EmbeddingModelOut]:
    models = await catalogue.active_models(ctx.session, kind=ModelKind.EMBEDDING)
    return [m for m in (_model_out(x) for x in models) if m is not None]


# ---- documents -----------------------------------------------------------------------


def _version_out(v: KnowledgeDocumentVersion) -> VersionOut:
    out = VersionOut.model_validate(v)
    out.downloadable = bool(v.object_key)
    return out


async def _latest_versions(
    ctx: TenantContext, docs: list[KnowledgeDocument]
) -> dict[uuid.UUID, KnowledgeDocumentVersion]:
    if not docs:
        return {}
    rows = (
        await ctx.session.execute(
            select(KnowledgeDocumentVersion).where(
                KnowledgeDocumentVersion.tenant_id == ctx.tenant_id,
                KnowledgeDocumentVersion.document_id.in_([d.id for d in docs]),
            )
        )
    ).scalars()
    latest: dict[uuid.UUID, KnowledgeDocumentVersion] = {}
    for v in rows:
        if v.document_id not in latest or v.version > latest[v.document_id].version:
            latest[v.document_id] = v
    return latest


def _document_out(doc: KnowledgeDocument, latest: KnowledgeDocumentVersion | None) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        title=doc.title,
        source_type=doc.source_type,
        source_url=doc.source_url,
        active_version=doc.active_version,
        latest=_version_out(latest) if latest else None,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def _detail(ctx: TenantContext, doc: KnowledgeDocument) -> DocumentDetail:
    await ctx.session.flush()
    await ctx.session.refresh(doc)  # timestamps set by the database
    versions = await service.versions(ctx.session, doc)
    base = _document_out(doc, versions[0] if versions else None)
    return DocumentDetail(**base.model_dump(), versions=[_version_out(v) for v in versions])


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(ctx: Reader) -> list[DocumentOut]:
    docs = list(
        (
            await ctx.session.execute(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.tenant_id == ctx.tenant_id)
                .order_by(KnowledgeDocument.created_at.desc())
            )
        ).scalars()
    )
    latest = await _latest_versions(ctx, docs)
    return [_document_out(d, latest.get(d.id)) for d in docs]


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    max_bytes = get_settings().knowledge_max_file_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValidationFailed(f"Files can be up to {max_bytes // (1024 * 1024)} MB.", code="file_too_large")
    filename = (file.filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:255]
    if not filename:
        raise ValidationFailed("The file has no name.", code="file_name_missing")
    return filename, data


async def _audit(ctx: TenantContext, meta: Meta, action: str, doc: KnowledgeDocument, **details: Any) -> None:
    await record(
        ctx.session,
        action,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user.id,
        meta=meta,
        target_type="knowledge_document",
        target_id=doc.id,
        details={"title": doc.title, **details},
    )


@router.post("/documents/upload", response_model=DocumentDetail, status_code=201)
async def upload(
    ctx: Manager,
    meta: Meta,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=200)] = None,
) -> DocumentDetail:
    await hit(f"knowledge_upload:{ctx.tenant_id}", UPLOADS_PER_MINUTE)
    filename, data = await _read_upload(file)
    try:
        file_type = detect(filename, data)
    except UnsupportedFile:
        raise
    doc, version = await service.add_file(
        ctx.session,
        ctx.tenant_id,
        ctx.user.id,
        title=(title or filename.rsplit(".", 1)[0] or filename).strip()[:200],
        filename=filename,
        file_type=file_type,
        data=data,
    )
    await _audit(
        ctx, meta, "knowledge.document_added", doc, source="file", version=version.version, size=len(data)
    )
    return await _detail(ctx, doc)


@router.post("/documents/url", response_model=DocumentDetail, status_code=201)
async def add_url(body: UrlIn, ctx: Manager, meta: Meta) -> DocumentDetail:
    await hit(f"knowledge_upload:{ctx.tenant_id}", UPLOADS_PER_MINUTE)
    doc, _ = await service.add_url(ctx.session, ctx.tenant_id, ctx.user.id, title=body.title, url=body.url)
    await _audit(ctx, meta, "knowledge.document_added", doc, source="url", url=doc.source_url)
    return await _detail(ctx, doc)


@router.post("/documents/text", response_model=DocumentDetail, status_code=201)
async def add_text(body: TextIn, ctx: Manager, meta: Meta) -> DocumentDetail:
    doc, version = await service.add_text(
        ctx.session, ctx.tenant_id, ctx.user.id, title=body.title, text=body.text
    )
    await _audit(ctx, meta, "knowledge.document_added", doc, source="text", version=version.version)
    return await _detail(ctx, doc)


@router.get("/documents/{document_id}", response_model=DocumentDetail)
async def get_document(document_id: uuid.UUID, ctx: Reader) -> DocumentDetail:
    return await _detail(ctx, await service.get_document(ctx.session, ctx.tenant_id, document_id))


@router.post("/documents/{document_id}/versions", response_model=DocumentDetail, status_code=201)
async def replace_file(
    document_id: uuid.UUID, ctx: Manager, meta: Meta, file: Annotated[UploadFile, File()]
) -> DocumentDetail:
    await hit(f"knowledge_upload:{ctx.tenant_id}", UPLOADS_PER_MINUTE)
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id, lock=True)
    filename, data = await _read_upload(file)
    file_type = detect(filename, data)
    _, version = await service.add_file(
        ctx.session,
        ctx.tenant_id,
        ctx.user.id,
        title=doc.title,
        filename=filename,
        file_type=file_type,
        data=data,
        document=doc,
    )
    await _audit(ctx, meta, "knowledge.version_added", doc, version=version.version, size=len(data))
    return await _detail(ctx, doc)


@router.put("/documents/{document_id}/text", response_model=DocumentDetail)
async def edit_text(document_id: uuid.UUID, body: TextIn, ctx: Manager, meta: Meta) -> DocumentDetail:
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id, lock=True)
    _, version = await service.add_text(
        ctx.session, ctx.tenant_id, ctx.user.id, title=body.title, text=body.text, document=doc
    )
    await _audit(ctx, meta, "knowledge.version_added", doc, version=version.version)
    return await _detail(ctx, doc)


@router.get("/documents/{document_id}/text")
async def note_text(document_id: uuid.UUID, ctx: Reader) -> dict[str, str]:
    """The current text of a note, for editing."""
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id)
    if doc.source_type != "text" or not doc.latest_version:
        raise ValidationFailed("Only notes can be edited as text.", code="wrong_source")
    version = await service.version_by_number(ctx.session, doc, doc.latest_version)
    if version is None or not version.object_key:
        raise NotFound("The note's text is not available.")
    data = await s3.get(version.object_key, 1024 * 1024)
    return {"title": doc.title, "text": data.decode("utf-8")}


@router.post("/documents/{document_id}/refresh", response_model=DocumentDetail)
async def refresh(document_id: uuid.UUID, ctx: Manager, meta: Meta) -> DocumentDetail:
    await hit(f"knowledge_upload:{ctx.tenant_id}", UPLOADS_PER_MINUTE)
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id, lock=True)
    version = await service.refresh_url(ctx.session, doc, ctx.user.id)
    await _audit(ctx, meta, "knowledge.version_added", doc, version=version.version, source="url")
    return await _detail(ctx, doc)


@router.post("/documents/{document_id}/versions/{number}/retry", response_model=DocumentDetail)
async def retry(document_id: uuid.UUID, number: int, ctx: Manager) -> DocumentDetail:
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id, lock=True)
    await service.retry(ctx.session, doc, number)
    return await _detail(ctx, doc)


@router.get("/documents/{document_id}/versions/{number}/download", response_model=DownloadOut)
async def download(document_id: uuid.UUID, number: int, ctx: Reader, meta: Meta) -> DownloadOut:
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id)
    version = await service.version_by_number(ctx.session, doc, number)
    if version is None or not version.object_key:
        raise NotFound("This version has no stored file.", code="no_file")
    name = version.original_filename or f"{doc.title}.{version.file_type or 'txt'}"
    url = await s3.download_url(version.object_key, name)
    await _audit(ctx, meta, "knowledge.version_downloaded", doc, version=number)
    return DownloadOut(url=url, expires_in=get_settings().s3_presign_seconds)


@router.delete("/documents/{document_id}/versions/{number}", response_model=DocumentDetail)
async def delete_version(document_id: uuid.UUID, number: int, ctx: Manager, meta: Meta) -> DocumentDetail:
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id, lock=True)
    await service.delete_version(ctx.session, doc, number)
    await _audit(ctx, meta, "knowledge.version_deleted", doc, version=number)
    return await _detail(ctx, doc)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: uuid.UUID, ctx: Manager, meta: Meta) -> None:
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id, lock=True)
    await _audit(ctx, meta, "knowledge.document_deleted", doc)
    await service.delete_document(ctx.session, doc)


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(
    document_id: uuid.UUID, ctx: Reader, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ChunkOut]:
    """What the assistant can read from this document (its active version)."""
    doc = await service.get_document(ctx.session, ctx.tenant_id, document_id)
    rows = (
        await ctx.session.execute(
            select(KnowledgeChunk)
            .join(KnowledgeDocumentVersion, KnowledgeDocumentVersion.id == KnowledgeChunk.version_id)
            .where(
                KnowledgeChunk.document_id == doc.id,
                KnowledgeDocumentVersion.status == VersionStatus.READY,
            )
            .order_by(KnowledgeChunk.ordinal)
            .limit(limit)
        )
    ).scalars()
    return [ChunkOut(ordinal=c.ordinal, heading=c.heading, content=c.content, meta=c.meta) for c in rows]


@router.post("/search", response_model=SearchOut)
async def search(body: SearchIn, ctx: Manager) -> SearchOut:
    """Try a customer question and see which passages the assistant would get."""
    await hit(f"knowledge_search:{ctx.tenant_id}", SEARCHES_PER_MINUTE)
    plan = await retrieval.plan_for(ctx.session, ctx.tenant_id)
    if isinstance(plan, str):
        row = await service.get_knowledge_settings(ctx.session, ctx.tenant_id)
        return SearchOut(passages=[], min_score=float(row.min_score) if row else 0.3, status=plan)
    vector, call, error = await retrieval.embed_query(plan, body.query, purpose="search_test")
    if call is not None:
        await catalogue.record_call(ctx.session, ctx.tenant_id, call)
    if vector is None:
        return SearchOut(passages=[], min_score=plan.min_score, status=error or "failed")
    # Show the closest passages even below the threshold, so the threshold can be tuned.
    passages = await retrieval.nearest(
        ctx.session, ctx.tenant_id, plan.embedder.model_pk, vector, top_k=plan.top_k, min_score=0.0
    )
    return SearchOut(
        passages=[
            PassageOut(
                document_id=p.document_id,
                title=p.title,
                content=p.content,
                score=p.score,
                meta=p.meta,
                used=p.score >= plan.min_score,
            )
            for p in passages
        ],
        min_score=plan.min_score,
        status="ok",
    )
