"""
backend/vectorstore/client.py
──────────────────────────────
Phase 3 Core Vector Database Client: RBAC-enforced Hybrid Search (dense + sparse
BM25) with per-leg timing, health-check, and collection statistics.

ARCHITECTURE DECISION: WHY HYBRID SEARCH IN HEALTHCARE RAG?
─────────────────────────────────────────────────────────
Dense-only retrieval fails on exact medical identifiers (CPT/ICD codes, policy
section numbers). Sparse BM25 fails on semantic paraphrases. Hybrid Search
runs BOTH legs concurrently and fuses via Reciprocal Rank Fusion (RRF) to
capture both lexical and semantic relevance — critical for insurance adjudication.

COMPLIANCE DECISION: PRE-RETRIEVAL RBAC AT THE VECTOR DB LAYER
───────────────────────────────────────────────────────────────────
Security CANNOT be delegated to the LLM. The RBAC `where` filter derived from
the JWT (tenant_id + role) is applied by ChromaDB BEFORE any vector scoring.
Vectors failing the filter are invisible to the similarity engine — documents
from Tenant B physically cannot appear in a query for Tenant A.
This is a compliance BOUNDARY, not a courtesy check.

BM25 IMPLEMENTATION NOTE
─────────────────────────
ChromaDB has no native BM25. The sparse leg fetches RBAC-filtered documents
and builds an in-memory BM25Okapi index. For >50K chunks/tenant, cache the
index in Redis or replace the sparse leg with Elasticsearch.

PER-LEG TIMING
───────────────
Because dense and sparse legs run concurrently via asyncio.gather(), measuring
their individual wall-clock times requires wrapping each coroutine in a timing
shell that records start/end before handing the result back to gather(). This
provides accurate per-leg latency in HybridSearchResult for observability
dashboards and SLO alerting.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import structlog
from rank_bm25 import BM25Okapi

from backend.database.vector_client import get_or_create_collection
from backend.rag.schemas import DocumentType

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RetrievedChunk:
    """
    Unified result from either the dense or sparse retrieval leg.

    Both legs produce this type so RRF fusion in retriever.py operates on
    a single unified interface without isinstance() branching.
    """
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    dense_score: float | None = None   # Cosine similarity [0,1]; None if sparse-only
    sparse_score: float | None = None  # BM25 score [0,∞); None if dense-only
    dense_rank: int | None = None      # 1-indexed rank in dense leg
    sparse_rank: int | None = None     # 1-indexed rank in sparse leg


@dataclass
class HybridSearchResult:
    """Output of hybrid_search() — passed directly to retriever.py for RRF fusion."""
    dense_results: list[RetrievedChunk]
    sparse_results: list[RetrievedChunk]
    collection_name: str
    query_text: str
    tenant_id: str
    user_role: str
    dense_latency_ms: float
    sparse_latency_ms: float
    total_latency_ms: float
    rbac_filter_applied: dict[str, Any]


# ─────────────────────────────────────────────────────────────────────────────
# RBAC Filter Builder
# ─────────────────────────────────────────────────────────────────────────────

def build_rbac_filter(
    tenant_id: str,
    user_role: str,
    document_types: list[DocumentType] | None = None,
    require_sanitized: bool = True,
) -> dict[str, Any]:
    """
    Build a ChromaDB `where` clause for pre-retrieval RBAC enforcement.

    Enforces two non-negotiable isolation boundaries:
      1. TENANT: tenant_id must match JWT-derived tenant_id exactly.
      2. ROLE:   chunk must have `role_{user_role}: True` flag (see flatten_roles_to_flags).

    ChromaDB metadata values are scalar (str/int/float/bool). Roles are stored
    as boolean flags (role_admin, role_underwriter, etc.) for reliable filtering
    since ChromaDB doesn't support $contains on list-typed metadata.

    Args:
        tenant_id:        From JWT — primary isolation key. Never user-supplied.
        user_role:        From JWT — role string (e.g., "claims_adjuster").
        document_types:   Optional document type scope filter.
        require_sanitized: If True, only return PHI-sanitized chunks. Always True in prod.
    """
    conditions: list[dict[str, Any]] = [
        {"tenant_id": {"$eq": tenant_id}},
        {f"role_{user_role}": {"$eq": True}},
    ]

    if require_sanitized:
        conditions.append({"is_sanitized": {"$eq": True}})

    if document_types:
        vals = [dt.value for dt in document_types]
        conditions.append(
            {"document_type": {"$eq": vals[0]}} if len(vals) == 1
            else {"document_type": {"$in": vals}}
        )

    return {"$and": conditions} if len(conditions) > 1 else conditions[0]


# ─────────────────────────────────────────────────────────────────────────────
# Role Flag Flattening (write path)
# ─────────────────────────────────────────────────────────────────────────────

ALL_PLATFORM_ROLES: frozenset[str] = frozenset(
    {"admin", "underwriter", "claims_adjuster", "insured"}
)


def flatten_roles_to_flags(allowed_roles: list[str]) -> dict[str, bool]:
    """
    Convert allowed_roles list → flat boolean metadata flags for ChromaDB.

    Example:
        ["admin", "underwriter"] → {"role_admin": True, "role_underwriter": True,
                                     "role_claims_adjuster": False, "role_insured": False}

    The False flags are essential — they ensure every chunk has all role keys,
    preventing KeyError / missing-key failures in ChromaDB where-clause evaluation.
    """
    return {f"role_{r}": (r in allowed_roles) for r in ALL_PLATFORM_ROLES}


def prepare_metadata_for_upsert(
    chunk_metadata: dict[str, Any],
    allowed_roles: list[str],
) -> dict[str, Any]:
    """
    Prepare chunk metadata for ChromaDB upsert.

    Keeps only ChromaDB-scalar-compatible fields (str/int/float/bool),
    adds boolean role flags, and drops list/None fields.
    """
    flat: dict[str, Any] = {
        k: v for k, v in chunk_metadata.items()
        if isinstance(v, (str, int, float, bool))
    }
    flat.update(flatten_roles_to_flags(allowed_roles))
    return flat


# ─────────────────────────────────────────────────────────────────────────────
# Dense Search
# ─────────────────────────────────────────────────────────────────────────────

async def dense_search(
    collection_name: str,
    query_embedding: list[float],
    rbac_filter: dict[str, Any],
    top_k: int = 20,
) -> list[RetrievedChunk]:
    """
    Dense ANN search via ChromaDB HNSW index (cosine similarity).

    The `rbac_filter` is applied by ChromaDB BEFORE similarity scoring —
    no out-of-scope vector is ever scored. This is the hard RBAC boundary.

    ChromaDB returns cosine DISTANCE [0, 2]. Converted to similarity:
        similarity = 1 - distance / 2
    """
    collection = await get_or_create_collection(collection_name)
    results = await collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=rbac_filter,
        include=["documents", "metadatas", "distances"],
    )

    chunks: list[RetrievedChunk] = []
    for rank, (doc_id, text, meta, dist) in enumerate(
        zip(
            results["ids"][0],
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ),
        start=1,
    ):
        chunks.append(RetrievedChunk(
            chunk_id=doc_id,
            text=text,
            metadata=meta,
            dense_score=max(0.0, 1.0 - dist / 2.0),
            dense_rank=rank,
        ))

    logger.debug("dense_search_complete", collection=collection_name, n=len(chunks))
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# BM25 Tokenizer (Healthcare-Aware)
# ─────────────────────────────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"[\s,;()\[\]{}|/\\]+")


def _tokenize_text(text: str) -> list[str]:
    """
    Tokenize text for BM25.

    Healthcare tokenization rules:
    • NO stemming — CPT/ICD codes must match exactly ("99213" ≠ "9921")
    • Preserve hyphens within tokens (CPT modifier "93000-26", ICD "Z51.11")
    • Lowercase normalization
    • Split on whitespace, commas, semicolons, parens, slashes
    """
    return [t for t in _SPLIT_RE.split(text.lower()) if t]


def _tokenize_corpus(texts: list[str]) -> list[list[str]]:
    return [_tokenize_text(t) for t in texts]


# ─────────────────────────────────────────────────────────────────────────────
# Sparse Search (BM25)
# ─────────────────────────────────────────────────────────────────────────────

async def sparse_search(
    collection_name: str,
    query_text: str,
    rbac_filter: dict[str, Any],
    top_k: int = 20,
) -> list[RetrievedChunk]:
    """
    Sparse BM25 keyword search with RBAC pre-filtering.

    Fetches the RBAC-filtered corpus from ChromaDB then builds an in-memory
    BM25Okapi index. BM25 is critical for exact matches on medical codes and
    policy identifiers that dense embeddings may paraphrase incorrectly.

    BM25 CPU work runs in asyncio.to_thread() to protect the event loop.

    Production path for large corpora (>50K chunks):
      Cache the BM25 index in Redis (pickle, 1h TTL) or replace with
      Elasticsearch as the sparse leg in a two-system hybrid architecture.
    """
    collection = await get_or_create_collection(collection_name)
    corpus = await collection.get(where=rbac_filter, include=["documents", "metadatas"])

    if not corpus["ids"]:
        logger.debug("sparse_search_empty_corpus", collection=collection_name)
        return []

    ids = corpus["ids"]
    texts = corpus["documents"]
    metas = corpus["metadatas"]

    # CPU-bound BM25 operations — run off the event loop
    tokenized = await asyncio.to_thread(_tokenize_corpus, texts)
    bm25 = await asyncio.to_thread(BM25Okapi, tokenized)
    query_tokens = _tokenize_text(query_text)
    scores = await asyncio.to_thread(bm25.get_scores, query_tokens)

    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    chunks: list[RetrievedChunk] = []
    for rank, idx in enumerate(ranked, start=1):
        if scores[idx] <= 0.0:
            break  # Remaining scores are zero — no term overlap
        chunks.append(RetrievedChunk(
            chunk_id=ids[idx],
            text=texts[idx],
            metadata=metas[idx],
            sparse_score=float(scores[idx]),
            sparse_rank=rank,
        ))

    logger.debug(
        "sparse_search_complete",
        collection=collection_name,
        corpus_size=len(ids),
        n=len(chunks),
    )
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Hybrid Search — Concurrent Dense + Sparse with RBAC
# ─────────────────────────────────────────────────────────────────────────────

async def hybrid_search(
    collection_name: str,
    query_text: str,
    query_embedding: list[float],
    tenant_id: str,
    user_role: str,
    dense_top_k: int = 20,
    sparse_top_k: int = 20,
    document_types: list[DocumentType] | None = None,
    require_sanitized: bool = True,
) -> HybridSearchResult:
    """
    Execute concurrent dense (ANN) + sparse (BM25) hybrid search with
    deterministic pre-retrieval RBAC filtering and accurate per-leg timing.

    Both search legs share the same RBAC filter — the same isolation guarantee
    applies regardless of whether a chunk is found via semantic or keyword match.

    Per-leg timing strategy:
      asyncio.gather() runs coroutines concurrently, so naive before/after
      measurement of the gather() call gives total wall-clock time, not
      individual leg times. We wrap each leg in a timing coroutine that
      captures its own start/end, then stores the duration in a shared dict
      BEFORE returning the result to gather(). This gives accurate individual
      leg latencies without blocking parallelism.

    Args:
        tenant_id:        JWT-derived tenant claim. NEVER accept from user input.
        user_role:        JWT-derived role claim.
        dense_top_k:      Candidates from the dense ANN leg.
        sparse_top_k:     Candidates from the BM25 sparse leg.
        document_types:   Optional scope filter by DocumentType.
        require_sanitized: Only retrieve HIPAA-sanitized chunks (always True in prod).

    Returns:
        HybridSearchResult with per-leg timings, passed to retriever.py for
        RRF fusion + cross-encoder re-ranking.
    """
    overall_start = time.perf_counter()

    rbac_filter = build_rbac_filter(
        tenant_id=tenant_id,
        user_role=user_role,
        document_types=document_types,
        require_sanitized=require_sanitized,
    )

    logger.info(
        "hybrid_search_started",
        collection=collection_name,
        tenant_id=tenant_id,
        user_role=user_role,
        rbac_conditions=len(rbac_filter.get("$and", [rbac_filter])),
    )

    # Per-leg timing containers — populated inside timing wrappers before
    # the gather() call returns so both values are always set.
    leg_timings: dict[str, float] = {"dense": 0.0, "sparse": 0.0}

    async def _timed_dense() -> list[RetrievedChunk]:
        t = time.perf_counter()
        result = await dense_search(
            collection_name, query_embedding, rbac_filter, dense_top_k
        )
        leg_timings["dense"] = (time.perf_counter() - t) * 1000
        return result

    async def _timed_sparse() -> list[RetrievedChunk]:
        t = time.perf_counter()
        result = await sparse_search(
            collection_name, query_text, rbac_filter, sparse_top_k
        )
        leg_timings["sparse"] = (time.perf_counter() - t) * 1000
        return result

    dense_results, sparse_results = await asyncio.gather(
        _timed_dense(),
        _timed_sparse(),
    )

    total_ms = (time.perf_counter() - overall_start) * 1000
    logger.info(
        "hybrid_search_complete",
        dense_hits=len(dense_results),
        sparse_hits=len(sparse_results),
        dense_ms=f"{leg_timings['dense']:.1f}",
        sparse_ms=f"{leg_timings['sparse']:.1f}",
        total_ms=f"{total_ms:.1f}",
    )

    return HybridSearchResult(
        dense_results=dense_results,
        sparse_results=sparse_results,
        collection_name=collection_name,
        query_text=query_text,
        tenant_id=tenant_id,
        user_role=user_role,
        dense_latency_ms=leg_timings["dense"],
        sparse_latency_ms=leg_timings["sparse"],
        total_latency_ms=total_ms,
        rbac_filter_applied=rbac_filter,
    )


# ───────────────────────────────────────────────────────────────────────────────
# Health Check & Collection Statistics
# ───────────────────────────────────────────────────────────────────────────────

async def health_check() -> dict[str, Any]:
    """
    Verify ChromaDB connectivity and return server metadata.

    Called during FastAPI startup (lifespan handler) and by the
    /health endpoint to confirm the vector database is reachable.
    Raises on connection failure so the health endpoint can return 503.

    Returns:
        dict with keys: status, server_version, collections, latency_ms.
    """
    from backend.database.vector_client import get_chroma_client

    start = time.perf_counter()
    client = get_chroma_client()
    try:
        # heartbeat() returns a nanosecond timestamp — verifies HTTP reachability
        await client.heartbeat()
        collections = await client.list_collections()
        latency_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "chromadb_health_ok",
            collections=len(collections),
            latency_ms=f"{latency_ms:.1f}",
        )
        return {
            "status": "ok",
            "collection_count": len(collections),
            "latency_ms": round(latency_ms, 2),
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.error("chromadb_health_failed", error=str(exc), latency_ms=f"{latency_ms:.1f}")
        raise RuntimeError(f"ChromaDB unreachable: {exc}") from exc


async def get_collection_stats(
    collection_name: str,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """
    Return document count and optional per-tenant count for a collection.

    Used by the pipeline orchestrator and monitoring dashboards to observe
    index growth over time. The optional tenant_id filter runs a ChromaDB
    `get()` with a metadata where-clause to count only that tenant's docs.

    Args:
        collection_name: ChromaDB collection to inspect.
        tenant_id:       If provided, count only this tenant's chunks.

    Returns:
        dict with keys: collection_name, total_count, tenant_count (if requested).
    """
    from backend.database.vector_client import get_or_create_collection

    collection = await get_or_create_collection(collection_name)
    total: int = await collection.count()

    stats: dict[str, Any] = {
        "collection_name": collection_name,
        "total_count": total,
    }

    if tenant_id:
        # Fetch only IDs (not documents) for minimal payload
        tenant_docs = await collection.get(
            where={"tenant_id": {"$eq": tenant_id}},
            include=[],  # IDs only
        )
        stats["tenant_count"] = len(tenant_docs["ids"])
        stats["tenant_id"] = tenant_id

    logger.debug("collection_stats", **stats)
    return stats


# ───────────────────────────────────────────────────────────────────────────────
# Upsert Helpers
# ───────────────────────────────────────────────────────────────────────────────

async def upsert_chunk(
    collection_name: str,
    chunk_id: str,
    text: str,
    embedding: list[float],
    chunk_metadata: dict[str, Any],
    allowed_roles: list[str],
) -> None:
    """Upsert a single DocumentChunk with RBAC-compatible flattened metadata."""
    collection = await get_or_create_collection(collection_name)
    await collection.upsert(
        ids=[chunk_id],
        documents=[text],
        embeddings=[embedding],
        metadatas=[prepare_metadata_for_upsert(chunk_metadata, allowed_roles)],
    )
    logger.debug("chunk_upserted", chunk_id=chunk_id)


async def batch_upsert_chunks(
    collection_name: str,
    chunks: list[dict[str, Any]],
    batch_size: int = 100,
) -> int:
    """
    Batch upsert chunks to ChromaDB in configurable batch sizes.

    Prevents memory pressure on the ChromaDB HTTP API for large ingestion jobs.
    Each chunk dict requires: chunk_id, text, embedding, chunk_metadata, allowed_roles.
    """
    collection = await get_or_create_collection(collection_name)
    total = 0

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        await collection.upsert(
            ids=[c["chunk_id"] for c in batch],
            documents=[c["text"] for c in batch],
            embeddings=[c["embedding"] for c in batch],
            metadatas=[
                prepare_metadata_for_upsert(c["chunk_metadata"], c["allowed_roles"])
                for c in batch
            ],
        )
        total += len(batch)
        logger.debug("batch_upsert_progress", upserted=total)

    logger.info("batch_upsert_complete", collection=collection_name, total=total)
    return total
