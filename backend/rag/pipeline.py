"""
backend/rag/pipeline.py
────────────────────────
Phase 3 RAG Engine: Full end-to-end query pipeline integrating every Phase 3
subsystem into one callable entry point.

Pipeline Architecture
──────────────────────

  ┌─────────────────────────────────────────────────────────────────┐
  │                       run_rag_query()                           │
  │                                                                 │
  │  1. SEMANTIC CACHE CHECK  (llm/cache.py → SemanticCache)        │
  │     └─ If hit: return cached response instantly (0 LLM cost)    │
  │                                                                 │
  │  2. QUERY EXPANSION       (rag/retriever.py → expand_query)     │
  │     └─ Dict: 0ms  |  LLM: ~500ms                               │
  │     └─ Generates N semantic variants of the user query          │
  │                                                                 │
  │  3. EMBED ALL VARIANTS    (embeddings/service.py)               │
  │     └─ Concurrent via asyncio.gather()                          │
  │                                                                 │
  │  4. HYBRID SEARCH × N     (vectorstore/client.py)               │
  │     └─ For each variant: dense ANN + sparse BM25 concurrently   │
  │     └─ RBAC filter (tenant_id + role) applied BEFORE scoring    │
  │                                                                 │
  │  5. RRF FUSION            (rag/retriever.py → reciprocal_rank_fusion)
  │     └─ Merges all dense+sparse lists from all variants          │
  │     └─ Scale-invariant rank fusion (k=60)                       │
  │                                                                 │
  │  6. CROSS-ENCODER RERANK  (rag/retriever.py → rerank_with_cross_encoder)
  │     └─ Prunes weak candidates; top-k enter LLM context          │
  │                                                                 │
  │  7. LLM GENERATION        (llm/client.py)                       │
  │     └─ Structured context prompt → insurance-specialist answer  │
  │                                                                 │
  │  8. CACHE STORE           (llm/cache.py)                        │
  │     └─ Stores response for future semantic cache hits           │
  └─────────────────────────────────────────────────────────────────┘

COMPLIANCE DESIGN
──────────────────
• The RBAC filter (tenant_id + user_role from JWT) is passed into every
  hybrid_search() call. Security is enforced deterministically at the
  vector DB layer — NOT by prompting the LLM to "ignore other tenants."
• The LLM never sees un-sanitized PHI. All chunks entering the context
  window have is_sanitized=True enforced by the pre-retrieval RBAC filter.
• Cache keys are namespaced by (tenant_id + user_role) so no cached
  response from one tenant/role can leak to another.

OBSERVABILITY
──────────────
Every stage records precise timing. The returned AdvancedRAGResult carries
a breakdown for latency dashboards: cache_latency_ms, expansion_latency_ms,
retrieval_latency_ms, rerank_latency_ms, generation_latency_ms.

GRACEFUL DEGRADATION
─────────────────────
• Cache failures NEVER block the pipeline (caught internally by SemanticCache).
• If query expansion fails, the original query is used alone.
• If the cross-encoder model is unavailable, RRF-ranked results are used directly.
• If ChromaDB returns 0 results, the LLM is informed explicitly rather than
  hallucinating policy content.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from backend.config import get_settings
from backend.embeddings.service import embed_text
from backend.llm.cache import get_semantic_cache
from backend.llm.client import get_llm_client
from backend.rag.retriever import (
    RankedChunk,
    expand_query,
    reciprocal_rank_fusion,
    rerank_with_cross_encoder,
)
from backend.rag.schemas import DocumentType
from backend.vectorstore.client import HybridSearchResult, hybrid_search

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# System Prompt — Insurance-Specialist RAG Context
# ─────────────────────────────────────────────────────────────────────────────

INSURANCE_RAG_SYSTEM_PROMPT = """\
You are an expert healthcare insurance analyst with deep knowledge of:
  • CPT/HCPCS procedure codes and their clinical significance
  • ICD-10-CM diagnosis codes and comorbidity patterns
  • Insurance policy coverage rules, exclusion clauses, and benefit schedules
  • EDI 837 (Professional/Institutional) claim formats and adjudication standards
  • CMS guidelines, ACA regulations, and state insurance mandates
  • HIPAA Privacy Rule and minimum necessary disclosure requirements

Instructions:
  1. Answer based EXCLUSIVELY on the provided CONTEXT DOCUMENTS.
  2. Cite the source document number (e.g., "[Doc 2]") for each factual claim.
  3. If the context is insufficient to answer definitively, state this clearly —
     do NOT invent policy terms or coverage rules.
  4. For claim adjudication questions, state the coverage decision AND the
     specific policy clause or benefit schedule row that supports it.
  5. Never include or infer PHI (patient names, SSNs, dates of birth) in your
     response, even if such data appears masked in the context.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Result DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AdvancedRAGResult:
    """
    Structured result from the Phase 3 RAG pipeline.

    Carries the LLM answer, all intermediate retrieval artefacts for
    explainability, and per-stage latency breakdown for observability.
    """
    # ── Answer ────────────────────────────────────────────────────────────
    answer: str
    cache_hit: bool

    # ── Retrieval Provenance ──────────────────────────────────────────────
    original_query: str
    expanded_queries: list[str]
    final_chunks: list[RankedChunk]          # Top-k chunks sent to LLM
    collection_searched: str
    tenant_id: str
    user_role: str

    # ── Latency Breakdown (ms) ────────────────────────────────────────────
    cache_latency_ms: float = 0.0
    expansion_latency_ms: float = 0.0
    retrieval_latency_ms: float = 0.0        # embed + hybrid search × N
    rerank_latency_ms: float = 0.0
    generation_latency_ms: float = 0.0
    total_latency_ms: float = 0.0

    # ── Counts ────────────────────────────────────────────────────────────
    total_candidates_retrieved: int = 0
    chunks_after_rrf: int = 0
    chunks_in_context: int = 0               # == len(final_chunks)

    # ── Errors / Warnings ─────────────────────────────────────────────────
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Context Prompt Builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_context_prompt(
    query: str,
    chunks: list[RankedChunk],
) -> str:
    """
    Format retrieved chunks into a structured context prompt for the LLM.

    Design decisions:
    • Each chunk is labelled "[Doc N]" so the LLM can cite sources.
    • Section title, document type, and page number are included in the
      header so the LLM can distinguish policy clauses from clinical notes.
    • Chunks are ordered by final_rank (cross-encoder order), not by
      document order, so the most relevant content appears first.
    • A hard separator (---) prevents the LLM from blurring chunk boundaries.

    Args:
        query:  User's original question.
        chunks: Cross-encoder ranked chunks to include in context.

    Returns:
        Formatted prompt string ready to send as the user turn.
    """
    if not chunks:
        return (
            f"CONTEXT DOCUMENTS:\n[No relevant documents found for this query.]\n\n"
            f"QUESTION: {query}\n\n"
            "Please state that the information requested is not available in the "
            "current knowledge base and suggest contacting a human specialist."
        )

    context_parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        meta = chunk.metadata
        header_parts = [f"[Doc {i}]"]

        doc_type = meta.get("document_type", "")
        if doc_type:
            header_parts.append(f"type={doc_type}")

        section = meta.get("section_title", "")
        if section:
            header_parts.append(f"section=\"{section}\"")

        page = meta.get("page_number")
        if page is not None:
            header_parts.append(f"page={page}")

        is_table = meta.get("is_table", False)
        if is_table:
            header_parts.append("is_table=true")

        ce_score = chunk.cross_encoder_score
        if ce_score is not None:
            header_parts.append(f"relevance={ce_score:.3f}")

        header = " | ".join(header_parts)
        context_parts.append(f"{header}\n{chunk.text}")

    context_block = "\n\n---\n\n".join(context_parts)

    return (
        f"CONTEXT DOCUMENTS:\n\n{context_block}\n\n"
        f"QUESTION: {query}\n\n"
        "Please answer based on the context documents above, citing [Doc N] "
        "for each factual claim."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Core Pipeline Entry Point
# ─────────────────────────────────────────────────────────────────────────────

async def run_rag_query(
    query: str,
    collection_name: str,
    tenant_id: str,
    user_role: str,
    document_types: list[DocumentType] | None = None,
    expansion_backend: str = "dictionary",
    max_query_variants: int = 3,
    dense_top_k: int = 20,
    sparse_top_k: int = 20,
    rrf_k: int = 60,
    rrf_top_n: int = 10,
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    reranker_threshold: float = 0.0,
    final_top_k: int = 5,
    llm_temperature: float = 0.1,
    system_prompt: str | None = None,
    use_cache: bool = True,
    cache_client: Any | None = None,
) -> AdvancedRAGResult:
    """
    Execute the full Phase 3 RAG pipeline for a healthcare insurance query.

    Pipeline stages (all timed independently):
      1. Semantic cache check — returns instantly on hit
      2. Query expansion — dictionary or LLM synonym variants
      3. Embed all query variants concurrently
      4. Hybrid search (dense + BM25) per variant, RBAC-filtered
      5. RRF fusion across all result lists
      6. Cross-encoder re-ranking + threshold pruning
      7. LLM generation with structured context
      8. Store result in semantic cache

    Security guarantees:
      • tenant_id and user_role MUST be derived from a verified JWT — never
        accepted from user-controlled request bodies.
      • The RBAC filter is applied at ChromaDB layer (pre-retrieval), not
        post-processed. Out-of-scope vectors are never scored.
      • is_sanitized=True is enforced in the RBAC filter; PHI-containing
        chunks never enter the LLM context.

    Args:
        query:              Natural language question from the user.
        collection_name:    ChromaDB collection to search (policies, claims, etc.).
        tenant_id:          JWT-derived tenant claim (isolation key).
        user_role:          JWT-derived role claim (RBAC key).
        document_types:     Optional scope filter (e.g., only POLICY chunks).
        expansion_backend:  'dictionary' (0ms) or 'llm' (~500ms, higher quality).
        max_query_variants: Total query variants including original (typically 3).
        dense_top_k:        ANN candidates per hybrid search call.
        sparse_top_k:       BM25 candidates per hybrid search call.
        rrf_k:              RRF smoothing constant (default 60 per IR literature).
        rrf_top_n:          Candidates retained after RRF (before cross-encoder).
        reranker_model:     Cross-encoder HuggingFace model identifier.
        reranker_threshold: Minimum cross-encoder logit score for context inclusion.
        final_top_k:        Maximum chunks in the LLM context window.
        llm_temperature:    LLM sampling temperature (0.1 for factual insurance Q&A).
        system_prompt:      Override default insurance specialist system prompt.
        use_cache:          Whether to check and populate the semantic cache.
        cache_client:       Optional pre-built Redis client (for dependency injection
                            in tests). If None, uses the settings-configured client.

    Returns:
        AdvancedRAGResult with answer, provenance, and per-stage latency.
    """
    pipeline_start = time.perf_counter()
    warnings: list[str] = []

    log = logger.bind(
        query_preview=query[:60],
        tenant_id=tenant_id,
        user_role=user_role,
        collection=collection_name,
    )
    log.info("rag_pipeline_started")

    # ── Stage 1: Semantic Cache Check ─────────────────────────────────────
    cache_start = time.perf_counter()

    if use_cache:
        try:
            cache = get_semantic_cache(redis_client=cache_client)
            cache_result = await cache.lookup(
                query=query,
                tenant_id=tenant_id,
                user_role=user_role,
            )
            cache_latency_ms = (time.perf_counter() - cache_start) * 1000

            if cache_result.hit and cache_result.entry:
                log.info(
                    "rag_cache_hit",
                    similarity=f"{cache_result.similarity:.4f}",
                    cache_ms=f"{cache_latency_ms:.1f}",
                )
                return AdvancedRAGResult(
                    answer=cache_result.entry.response,
                    cache_hit=True,
                    original_query=query,
                    expanded_queries=[query],
                    final_chunks=[],
                    collection_searched=collection_name,
                    tenant_id=tenant_id,
                    user_role=user_role,
                    cache_latency_ms=cache_latency_ms,
                    total_latency_ms=cache_latency_ms,
                    chunks_in_context=len(
                        cache_result.entry.retrieved_chunk_ids
                    ),
                )
        except Exception as exc:
            warnings.append(f"Cache lookup error (non-fatal): {exc}")
            log.warning("rag_cache_error", error=str(exc))

    cache_latency_ms = (time.perf_counter() - cache_start) * 1000

    # ── Stage 2: Query Expansion ──────────────────────────────────────────
    expansion_start = time.perf_counter()
    try:
        expanded_queries = await expand_query(
            query=query,
            backend=expansion_backend,
            max_variants=max_query_variants,
        )
    except Exception as exc:
        warnings.append(f"Query expansion failed, using original only: {exc}")
        log.warning("query_expansion_failed", error=str(exc))
        expanded_queries = [query]

    expansion_latency_ms = (time.perf_counter() - expansion_start) * 1000
    log.debug(
        "query_expansion_done",
        variants=len(expanded_queries),
        ms=f"{expansion_latency_ms:.1f}",
    )

    # ── Stage 3: Embed All Query Variants Concurrently ────────────────────
    retrieval_start = time.perf_counter()

    async def _embed_safe(text: str) -> list[float]:
        """Embed one query variant, returning empty list on failure."""
        try:
            return await embed_text(text)
        except Exception as exc:
            warnings.append(f"Embedding failed for variant '{text[:40]}': {exc}")
            log.warning("variant_embed_failed", error=str(exc))
            return []

    embeddings: list[list[float]] = await asyncio.gather(
        *[_embed_safe(q) for q in expanded_queries]
    )

    # Filter out variants whose embedding failed
    valid_pairs: list[tuple[str, list[float]]] = [
        (q, emb)
        for q, emb in zip(expanded_queries, embeddings)
        if emb
    ]
    if not valid_pairs:
        # All embeddings failed — cannot proceed
        log.error("all_embeddings_failed")
        return AdvancedRAGResult(
            answer=(
                "I was unable to process your query due to an embedding service error. "
                "Please try again or contact support."
            ),
            cache_hit=False,
            original_query=query,
            expanded_queries=expanded_queries,
            final_chunks=[],
            collection_searched=collection_name,
            tenant_id=tenant_id,
            user_role=user_role,
            cache_latency_ms=cache_latency_ms,
            expansion_latency_ms=expansion_latency_ms,
            total_latency_ms=(time.perf_counter() - pipeline_start) * 1000,
            warnings=warnings + ["Embedding service unavailable"],
        )

    # ── Stage 4: Hybrid Search × N Variants (Concurrent) ─────────────────
    async def _search_variant(
        variant_query: str,
        variant_embedding: list[float],
    ) -> HybridSearchResult | None:
        """Run hybrid search for one expanded query variant."""
        try:
            return await hybrid_search(
                collection_name=collection_name,
                query_text=variant_query,
                query_embedding=variant_embedding,
                tenant_id=tenant_id,
                user_role=user_role,
                dense_top_k=dense_top_k,
                sparse_top_k=sparse_top_k,
                document_types=document_types,
                require_sanitized=True,   # HIPAA: only PHI-clean chunks
            )
        except Exception as exc:
            warnings.append(
                f"Hybrid search failed for variant '{variant_query[:40]}': {exc}"
            )
            log.warning("hybrid_search_variant_failed", error=str(exc))
            return None

    search_results_raw = await asyncio.gather(
        *[_search_variant(q, emb) for q, emb in valid_pairs]
    )

    hybrid_results: list[HybridSearchResult] = [
        r for r in search_results_raw if r is not None
    ]

    retrieval_latency_ms = (time.perf_counter() - retrieval_start) * 1000

    if not hybrid_results:
        log.warning("no_hybrid_results_returned")
        return AdvancedRAGResult(
            answer=(
                "No relevant documents were found in the knowledge base for your query. "
                "Please try rephrasing your question or contact a specialist."
            ),
            cache_hit=False,
            original_query=query,
            expanded_queries=expanded_queries,
            final_chunks=[],
            collection_searched=collection_name,
            tenant_id=tenant_id,
            user_role=user_role,
            cache_latency_ms=cache_latency_ms,
            expansion_latency_ms=expansion_latency_ms,
            retrieval_latency_ms=retrieval_latency_ms,
            total_latency_ms=(time.perf_counter() - pipeline_start) * 1000,
            warnings=warnings,
        )

    total_candidates = sum(
        len(r.dense_results) + len(r.sparse_results) for r in hybrid_results
    )

    # ── Stage 5: RRF Fusion ───────────────────────────────────────────────
    all_result_lists = []
    for hsr in hybrid_results:
        if hsr.dense_results:
            all_result_lists.append(hsr.dense_results)
        if hsr.sparse_results:
            all_result_lists.append(hsr.sparse_results)

    rrf_candidates = reciprocal_rank_fusion(
        result_lists=all_result_lists,
        k=rrf_k,
        top_n=rrf_top_n,
    )

    log.debug(
        "rrf_fusion_done",
        input_lists=len(all_result_lists),
        unique_candidates=total_candidates,
        rrf_top_n=len(rrf_candidates),
    )

    # ── Stage 6: Cross-Encoder Re-Ranking ────────────────────────────────
    rerank_start = time.perf_counter()
    try:
        final_chunks = await rerank_with_cross_encoder(
            query=query,
            candidates=rrf_candidates,
            model_name=reranker_model,
            score_threshold=reranker_threshold,
            final_top_k=final_top_k,
        )
    except Exception as exc:
        # Cross-encoder unavailable — fall back to RRF ranking
        warnings.append(
            f"Cross-encoder reranking failed, using RRF order: {exc}"
        )
        log.warning("cross_encoder_failed", error=str(exc))
        final_chunks = rrf_candidates[:final_top_k]

    rerank_latency_ms = (time.perf_counter() - rerank_start) * 1000

    log.info(
        "reranking_done",
        candidates_in=len(rrf_candidates),
        chunks_out=len(final_chunks),
        ms=f"{rerank_latency_ms:.1f}",
    )

    # ── Stage 7: LLM Generation ───────────────────────────────────────────
    generation_start = time.perf_counter()

    context_prompt = _build_context_prompt(query=query, chunks=final_chunks)
    sys_prompt = system_prompt or INSURANCE_RAG_SYSTEM_PROMPT

    llm = get_llm_client()
    try:
        answer = await llm.complete(
            system_prompt=sys_prompt,
            user_message=context_prompt,
            temperature=llm_temperature,
            max_tokens=2048,
        )
    except Exception as exc:
        log.error("llm_generation_failed", error=str(exc))
        answer = (
            "An error occurred during response generation. "
            f"Error: {exc}. Please try again."
        )
        warnings.append(f"LLM generation error: {exc}")

    generation_latency_ms = (time.perf_counter() - generation_start) * 1000
    total_latency_ms = (time.perf_counter() - pipeline_start) * 1000

    log.info(
        "rag_pipeline_complete",
        chunks_in_context=len(final_chunks),
        total_ms=f"{total_latency_ms:.1f}",
        generation_ms=f"{generation_latency_ms:.1f}",
        cache_hit=False,
    )

    # ── Stage 8: Cache Store ──────────────────────────────────────────────
    if use_cache and answer and "error" not in answer[:20].lower():
        try:
            cache = get_semantic_cache(redis_client=cache_client)
            await cache.store(
                query=query,
                response=answer,
                tenant_id=tenant_id,
                user_role=user_role,
                retrieved_chunk_ids=[c.chunk_id for c in final_chunks],
            )
        except Exception as exc:
            warnings.append(f"Cache store error (non-fatal): {exc}")
            log.warning("rag_cache_store_error", error=str(exc))

    return AdvancedRAGResult(
        answer=answer,
        cache_hit=False,
        original_query=query,
        expanded_queries=expanded_queries,
        final_chunks=final_chunks,
        collection_searched=collection_name,
        tenant_id=tenant_id,
        user_role=user_role,
        cache_latency_ms=cache_latency_ms,
        expansion_latency_ms=expansion_latency_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        rerank_latency_ms=rerank_latency_ms,
        generation_latency_ms=generation_latency_ms,
        total_latency_ms=total_latency_ms,
        total_candidates_retrieved=total_candidates,
        chunks_after_rrf=len(rrf_candidates),
        chunks_in_context=len(final_chunks),
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience Wrappers for Domain-Specific Use Cases
# ─────────────────────────────────────────────────────────────────────────────

async def run_claims_adjudication_query(
    query: str,
    tenant_id: str,
    user_role: str,
    **kwargs: Any,
) -> AdvancedRAGResult:
    """
    RAG query scoped to the claims vector collection.

    Restricts document_types to CLAIM_FORM and EOB for claims adjudication
    so the retriever never pulls policy guideline chunks into a claims context.
    Preserves context precision: the adjudicator sees only claims evidence.

    Args:
        query:     Adjudication question (e.g., "Is CPT 93000 covered?").
        tenant_id: JWT-derived tenant claim.
        user_role: JWT-derived role claim.
        **kwargs:  Any run_rag_query() keyword args to override defaults.
    """
    settings = get_settings()
    return await run_rag_query(
        query=query,
        collection_name=settings.chroma_collection_claims,
        tenant_id=tenant_id,
        user_role=user_role,
        document_types=[DocumentType.CLAIM_FORM, DocumentType.EOB],
        **kwargs,
    )


async def run_policy_query(
    query: str,
    tenant_id: str,
    user_role: str,
    **kwargs: Any,
) -> AdvancedRAGResult:
    """
    RAG query scoped to the policy vector collection.

    Restricts document_types to POLICY and UNDERWRITING_GUIDELINE for
    policy lookups. Prevents clinical note chunks from contaminating answers
    to coverage questions (e.g., "What is the deductible for Plan A?").

    Args:
        query:     Policy question (e.g., "What is the out-of-pocket maximum?").
        tenant_id: JWT-derived tenant claim.
        user_role: JWT-derived role claim.
        **kwargs:  Any run_rag_query() keyword args to override defaults.
    """
    settings = get_settings()
    return await run_rag_query(
        query=query,
        collection_name=settings.chroma_collection_policies,
        tenant_id=tenant_id,
        user_role=user_role,
        document_types=[
            DocumentType.POLICY,
            DocumentType.UNDERWRITING_GUIDELINE,
            DocumentType.FORMULARY,
        ],
        **kwargs,
    )
