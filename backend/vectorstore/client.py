"""
backend/vectorstore/client.py
──────────────────────────────
Core Vector Database client: RBAC-enforced Hybrid Search (dense + sparse BM25).

ARCHITECTURE DECISION: WHY HYBRID SEARCH IN HEALTHCARE RAG?
─────────────────────────────────────────────────────────────
Dense-only retrieval fails on exact medical identifiers (CPT/ICD codes, policy
section numbers). Sparse BM25 fails on semantic paraphrases. Hybrid Search
runs BOTH legs concurrently and fuses via Reciprocal Rank Fusion (RRF) to
capture both lexical and semantic relevance — critical for insurance adjudication.

COMPLIANCE DECISION: PRE-RETRIEVAL RBAC AT THE VECTOR DB LAYER
───────────────────────────────────────────────────────────────
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
    deterministic pre-retrieval RBAC filtering.

    Both search legs share the same RBAC filter — the same isolation guarantee
    applies regardless of whether a chunk is found via semantic or keyword match.

    Args:
        tenant_id:  JWT-derived tenant claim. NEVER accept from user input.
        user_role:  JWT-derived role claim.
        (other args): See dense_search() / sparse_search().

    Returns:
        HybridSearchResult passed to retriever.py for RRF fusion + reranking.
    """
    start = time.perf_counter()

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
    )

    dense_results, sparse_results = await asyncio.gather(
        dense_search(collection_name, query_embedding, rbac_filter, dense_top_k),
        sparse_search(collection_name, query_text, rbac_filter, sparse_top_k),
    )

    total_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "hybrid_search_complete",
        dense=len(dense_results),
        sparse=len(sparse_results),
        ms=f"{total_ms:.1f}",
    )

    return HybridSearchResult(
        dense_results=dense_results,
        sparse_results=sparse_results,
        collection_name=collection_name,
        query_text=query_text,
        tenant_id=tenant_id,
        user_role=user_role,
        dense_latency_ms=0.0,
        sparse_latency_ms=0.0,
        total_latency_ms=total_ms,
        rbac_filter_applied=rbac_filter,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Upsert Helpers
# ─────────────────────────────────────────────────────────────────────────────

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
