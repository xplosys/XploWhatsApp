from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.ai.models import ProviderKey


class EmbeddingModelOut(BaseModel):
    id: uuid.UUID
    provider: str
    model_id: str
    display_name: str | None


class KnowledgeUsageOut(BaseModel):
    documents: int
    max_documents: int
    storage_bytes: int
    max_storage_bytes: int


class SettingsOut(BaseModel):
    embedding_model: EmbeddingModelOut | None
    pending_embedding_model: EmbeddingModelOut | None
    reindex_error: str | None
    reindex_done: int
    reindex_total: int
    min_score: Decimal
    top_k: int
    usage: KnowledgeUsageOut
    storage_ready: bool
    virus_scan: bool
    max_file_mb: int


class SettingsUpdate(BaseModel):
    embedding_provider: ProviderKey | None = None
    embedding_model: str | None = Field(default=None, min_length=1, max_length=200)
    min_score: Decimal | None = Field(default=None, ge=0, le=1, max_digits=4, decimal_places=3)
    top_k: int | None = Field(default=None, ge=1, le=20)


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    status: str
    original_filename: str | None
    file_type: str | None
    size_bytes: int
    scan_result: str | None
    error_code: str | None
    error: str | None
    char_count: int
    chunk_count: int
    created_at: datetime
    processed_at: datetime | None
    downloadable: bool = False


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    source_type: str
    source_url: str | None
    active_version: int | None
    latest: VersionOut | None
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentOut):
    versions: list[VersionOut]


class UrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    title: str | None = Field(default=None, max_length=200)


class TextIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=200_000)


class DownloadOut(BaseModel):
    url: str
    expires_in: int


class ChunkOut(BaseModel):
    ordinal: int
    heading: str | None
    content: str
    meta: dict[str, Any]


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=1500)


class PassageOut(BaseModel):
    document_id: uuid.UUID
    title: str
    content: str
    score: float
    meta: dict[str, Any]
    used: bool  # at or above the threshold, so the assistant would get it


class SearchOut(BaseModel):
    passages: list[PassageOut]
    min_score: float
    status: str  # ok | not_configured | empty | <error code>
