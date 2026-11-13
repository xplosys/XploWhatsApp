"""Knowledge base: documents, their versions, text chunks and chunk embeddings.

A document (a file, a web page or a typed note) has numbered versions. Each version is
stored in object storage, scanned, extracted and split into chunks. Only the document's
active version keeps chunks, so answers always come from the current text.

Chunk text and chunk vectors live in separate tables so the business can switch
embedding model without re-reading its files: the new model's vectors are added next
to the old ones, and the switch happens when every chunk has one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Vector size is fixed by the column type; every embedding model is asked for this size.
EMBEDDING_DIMENSIONS = 1536


class SourceType(StrEnum):
    FILE = "file"
    URL = "url"
    TEXT = "text"


class VersionStatus(StrEnum):
    QUEUED = "queued"  # stored, waiting for the worker
    PROCESSING = "processing"  # scanning, extracting, embedding
    READY = "ready"  # active version with chunks
    REPLACED = "replaced"  # a newer version became active; chunks removed, file kept
    FAILED = "failed"  # could not be read or embedded; can be retried
    REJECTED = "rejected"  # malware found; file deleted


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    CSV = "csv"
    TXT = "txt"
    MARKDOWN = "md"
    HTML = "html"


class KnowledgeSettings(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """Per-business retrieval settings. `embedding_model_pk` is the model whose vectors
    answers are searched with; `pending_embedding_model_pk` is being indexed."""

    __tablename__ = "knowledge_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id"),
        CheckConstraint("min_score >= 0 AND min_score <= 1", name="min_score_range"),
        CheckConstraint("top_k BETWEEN 1 AND 20", name="top_k_range"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    embedding_model_pk: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="RESTRICT")
    )
    pending_embedding_model_pk: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="RESTRICT")
    )
    reindex_error: Mapped[str | None] = mapped_column(String(500))
    min_score: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.300"), server_default="0.300"
    )
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("source_type IN ('file', 'url', 'text')", name="source_type_valid"),
        Index("ix_knowledge_documents_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(8), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    active_version: Mapped[int | None] = mapped_column(Integer)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class KnowledgeDocumentVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["knowledge_documents.tenant_id", "knowledge_documents.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'replaced', 'failed', 'rejected')",
            name="status_valid",
        ),
        Index("ix_knowledge_document_versions_status", "status", "updated_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=VersionStatus.QUEUED)
    # Set once the file is stored. A URL version gets its object after the page is fetched.
    object_key: Mapped[str | None] = mapped_column(String(300))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    file_type: Mapped[str | None] = mapped_column(String(8))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    scan_result: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["knowledge_document_versions.tenant_id", "knowledge_document_versions.id"],
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_chunks_tenant_document", "tenant_id", "document_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class KnowledgeEmbedding(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "knowledge_chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "model_pk"),
        ForeignKeyConstraint(
            ["tenant_id", "chunk_id"],
            ["knowledge_chunks.tenant_id", "knowledge_chunks.id"],
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_chunk_embeddings_tenant_model", "tenant_id", "model_pk"),
        Index(
            "ix_knowledge_chunk_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="RESTRICT"), nullable=False
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
