"""Finding the knowledge passages that match a customer's question.

The question is embedded with the business's active embedding model (no database
transaction open), then the nearest chunks are found with pgvector's HNSW index,
limited to this business and this model, and kept only above the business's
similarity threshold.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.catalogue import CallRecord
from app.modules.ai.prompt import Passage
from app.modules.ai.providers.base import Turn
from app.modules.knowledge import embeddings
from app.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument, KnowledgeEmbedding
from app.modules.knowledge.service import get_knowledge_settings

QUERY_MAX_CHARS = 1500


@dataclass(frozen=True)
class RetrievalPlan:
    embedder: embeddings.Embedder
    top_k: int
    min_score: float


@dataclass
class Retrieved:
    passages: list[Passage]
    call: CallRecord | None
    summary: dict[str, object]


async def plan_for(session: AsyncSession, tenant_id: uuid.UUID) -> RetrievalPlan | str:
    """How to search this business's knowledge, or why it can't be searched."""
    row = await get_knowledge_settings(session, tenant_id)
    if row is None or row.embedding_model_pk is None:
        return "not_configured"
    has_chunks = await session.scalar(
        select(KnowledgeEmbedding.id)
        .where(
            KnowledgeEmbedding.tenant_id == tenant_id, KnowledgeEmbedding.model_pk == row.embedding_model_pk
        )
        .limit(1)
    )
    if has_chunks is None:
        return "empty"
    try:
        embedder = await embeddings.embedder_for(session, tenant_id, row.embedding_model_pk)
    except embeddings.EmbedderUnavailable as exc:
        return exc.code
    return RetrievalPlan(embedder, row.top_k, float(row.min_score))


def queries_from_turns(turns: list[Turn]) -> list[str]:
    """What to search for: the customer's latest message, and also their last few
    messages together, so a follow-up like "and on Sunday?" still finds the passage
    the earlier question was about, while a change of topic isn't diluted by it."""
    user_texts = [t.text.strip() for t in turns if t.role == "user" and t.text.strip()]
    if not user_texts:
        return []
    latest = user_texts[-1][-QUERY_MAX_CHARS:]
    combined = "\n".join(user_texts[-3:])[-QUERY_MAX_CHARS:]
    return [latest] if combined == latest else [latest, combined]


async def embed_queries(
    plan: RetrievalPlan, queries: list[str], purpose: str
) -> tuple[list[list[float]] | None, CallRecord | None, str | None]:
    """One embedding call for all the queries."""
    queries = [q for q in queries if q.strip()]
    if not queries:
        return None, None, "empty_query"
    try:
        outcome = await embeddings.embed(plan.embedder, queries, task="query", purpose=purpose)
    except embeddings.EmbedFailed as exc:
        return None, exc.call, exc.code
    return outcome.vectors, outcome.call, None


async def embed_query(
    plan: RetrievalPlan, query: str, purpose: str
) -> tuple[list[float] | None, CallRecord | None, str | None]:
    vectors, call, error = await embed_queries(plan, [query], purpose)
    return (vectors[0] if vectors else None), call, error


async def nearest(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    model_pk: uuid.UUID,
    vector: list[float],
    *,
    top_k: int,
    min_score: float,
) -> list[Passage]:
    # Keep scanning the HNSW graph until enough rows pass the tenant and model filter.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
    distance = KnowledgeEmbedding.embedding.cosine_distance(vector).label("distance")
    rows = (
        await session.execute(
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.document_id,
                KnowledgeChunk.content,
                KnowledgeChunk.meta,
                KnowledgeDocument.title,
                distance,
            )
            .select_from(KnowledgeEmbedding)
            .join(KnowledgeChunk, KnowledgeChunk.id == KnowledgeEmbedding.chunk_id)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(KnowledgeEmbedding.tenant_id == tenant_id, KnowledgeEmbedding.model_pk == model_pk)
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    passages = [
        Passage(r.id, r.document_id, r.title, r.content, 1.0 - float(r.distance), r.meta or {})
        for r in rows
        if 1.0 - float(r.distance) >= min_score
    ]
    # relaxed_order may return rows slightly out of order.
    passages.sort(key=lambda p: p.score, reverse=True)
    return passages


async def nearest_any(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    model_pk: uuid.UUID,
    vectors: list[list[float]],
    *,
    top_k: int,
    min_score: float,
) -> list[Passage]:
    """The best passages for any of the query vectors, each scored by its best match."""
    best: dict[uuid.UUID, Passage] = {}
    for vector in vectors:
        for p in await nearest(session, tenant_id, model_pk, vector, top_k=top_k, min_score=min_score):
            if p.chunk_id not in best or p.score > best[p.chunk_id].score:
                best[p.chunk_id] = p
    return sorted(best.values(), key=lambda p: p.score, reverse=True)[:top_k]


async def search(
    session: AsyncSession, tenant_id: uuid.UUID, plan: RetrievalPlan, queries: list[str], purpose: str
) -> Retrieved:
    """Embed and search in one go. Holds the caller's transaction during the provider
    call, so it is meant for interactive requests (preview, search test)."""
    vectors, call, error = await embed_queries(plan, queries, purpose)
    if vectors is None:
        return Retrieved([], call, {"hits": 0, "error": error})
    passages = await nearest_any(
        session, tenant_id, plan.embedder.model_pk, vectors, top_k=plan.top_k, min_score=plan.min_score
    )
    return Retrieved(passages, call, {"hits": len(passages)})
