"""
backend/rag/retriever.py
─────────────────────────
Query Expansion + Multi-Stage RRF Re-Ranking pipeline.

QUERY EXPANSION
───────────────
Medical terminology is highly polysemous and domain-specific. A clinician
asking "what is covered for CHF" and an insured member asking "is heart
failure treatment covered" are asking the same question but using different
vocabularies. Dense embeddings partially bridge this gap, but BM25 (the sparse
leg) is entirely keyword-dependent.

Query expansion generates synonym variants of the user's query BEFORE
retrieval. This dramatically improves BM25 recall by ensuring that both the
clinical term ("myocardial infarction") and the lay term ("heart attack") are
submitted to the keyword index.

Two backends are implemented:
  1. DICTIONARY (default): Zero-latency. Healthcare-specific synonym dictionary
     with CPT/ICD-10/condition/drug equivalence mappings. No external calls.
  2. LLM: Higher quality. Submits the query to the LLM for contextual expansion.
     ~400-600ms overhead. Recommended for complex multi-concept queries.

RECIPROCAL RANK FUSION (RRF)
─────────────────────────────
RRF fuses ranked lists from multiple retrieval systems without requiring score
normalization. The formula for each document d across systems is:

    RRF_score(d) = Σ  1 / (k + rank(d, system_i))
                 i ∈ systems

Where k is a smoothing constant (typically 60). If a document doesn't appear
in a system's results, it contributes 0 to that term.

Why RRF over score normalization?
  • Dense scores (cosine similarity) and sparse scores (BM25) are not
    on the same scale — you cannot meaningfully add them directly.
  • RRF only uses RANK, not raw score, making it scale-invariant.
  • Empirically, RRF outperforms linear combination on TREC benchmarks.
  • k=60 is the standard value proven across 100+ IR experiments.

CROSS-ENCODER RE-RANKING
─────────────────────────
After RRF fusion, the top-N candidates are re-scored by a cross-encoder model.

A bi-encoder (used for embedding) embeds query and document independently,
then scores their similarity. A cross-encoder encodes query + document JOINTLY,
attending to interactions between them. This joint attention is much more
accurate but ~100x slower — making it unsuitable for first-stage retrieval
over millions of vectors.

The two-stage approach:
  Stage 1: Fast bi-encoder retrieval (ANN) + BM25 → top 20-40 candidates
  Stage 2: Slow cross-encoder re-scoring → top 5 final context chunks

This achieves near-oracle precision on the final context while maintaining
sub-second end-to-end latency for the user.

Model choice: `cross-encoder/ms-marco-MiniLM-L-6-v2`
  • Trained on MS MARCO passage retrieval — strong general relevance scoring
  • 6-layer MiniLM — 22M params, ~50ms for 10 query-chunk pairs on CPU
  • Scores in [-10, +10] range (logit space). Threshold of 0.0 prunes weak matches.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from backend.vectorstore.client import HybridSearchResult, RetrievedChunk

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RankedChunk:
    """
    A DocumentChunk after RRF fusion and cross-encoder re-ranking.
    This is the final object passed to the LLM context builder.
    """
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    rrf_score: float           # RRF fusion score (higher = better)
    rrf_rank: int              # Rank after RRF (1-indexed)
    cross_encoder_score: float | None = None  # Cross-encoder logit score
    final_rank: int | None = None             # Rank after cross-encoder pruning
    dense_rank: int | None = None
    sparse_rank: int | None = None
    query_variant_matched: str | None = None  # Which expanded variant retrieved this


@dataclass
class RetrievalResult:
    """Complete output of the retrieval pipeline for one query."""
    original_query: str
    expanded_queries: list[str]
    final_chunks: list[RankedChunk]          # Top-k after all stages; ready for LLM
    total_candidates_before_rerank: int
    total_candidates_after_rerank: int
    cache_hit: bool = False
    retrieval_latency_ms: float = 0.0
    rerank_latency_ms: float = 0.0
    expansion_latency_ms: float = 0.0
    total_latency_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Healthcare Synonym Dictionary (Query Expansion — Dictionary Backend)
# ─────────────────────────────────────────────────────────────────────────────

# Structured as: {canonical_term: [synonym_1, synonym_2, ...]}
# Terms are lower-cased for matching. Expansion is bidirectional:
# matching "CHF" expands to include "heart failure" AND matching "heart failure"
# expands to include "CHF".
HEALTHCARE_SYNONYM_DICT: dict[str, list[str]] = {
    # Cardiovascular
    "heart failure": ["chf", "congestive heart failure", "cardiac failure", "hf", "lvsd"],
    "chf": ["heart failure", "congestive heart failure", "cardiac failure"],
    "myocardial infarction": ["heart attack", "mi", "stemi", "nstemi", "ami", "acute mi"],
    "heart attack": ["myocardial infarction", "mi", "stemi", "acute coronary syndrome"],
    "hypertension": ["high blood pressure", "htn", "elevated blood pressure", "arterial hypertension"],
    "afib": ["atrial fibrillation", "a-fib", "af", "irregular heartbeat"],
    "atrial fibrillation": ["afib", "a-fib", "af", "auricular fibrillation"],
    "coronary artery disease": ["cad", "coronary heart disease", "chd", "ischemic heart disease"],

    # Respiratory
    "copd": ["chronic obstructive pulmonary disease", "emphysema", "chronic bronchitis"],
    "asthma": ["reactive airway disease", "bronchospasm", "asthmatic"],
    "pneumonia": ["lung infection", "pna", "lower respiratory infection", "pulmonary infection"],
    "pulmonary embolism": ["pe", "blood clot in lung", "pulmonary thromboembolism"],

    # Metabolic
    "diabetes": ["dm", "diabetes mellitus", "t2dm", "type 2 diabetes", "hyperglycemia"],
    "type 2 diabetes": ["t2dm", "diabetes mellitus type 2", "non-insulin dependent diabetes"],
    "obesity": ["bmi over 30", "morbid obesity", "overweight", "elevated bmi"],
    "hypothyroidism": ["underactive thyroid", "low thyroid", "thyroid deficiency"],

    # Oncology
    "cancer": ["malignancy", "neoplasm", "carcinoma", "tumor", "oncology"],
    "chemotherapy": ["chemo", "cytotoxic therapy", "antineoplastic therapy"],
    "radiation therapy": ["radiotherapy", "xrt", "radiation treatment", "rt"],

    # Mental Health
    "depression": ["major depressive disorder", "mdd", "depressive disorder", "clinical depression"],
    "anxiety": ["gad", "generalized anxiety disorder", "anxiety disorder", "panic disorder"],

    # Musculoskeletal
    "knee replacement": ["total knee arthroplasty", "tka", "knee arthroplasty", "tkr"],
    "hip replacement": ["total hip arthroplasty", "tha", "hip arthroplasty"],

    # Insurance-specific
    "deductible": ["annual deductible", "plan deductible", "individual deductible"],
    "copay": ["co-payment", "copayment", "co pay"],
    "coinsurance": ["co-insurance", "cost sharing", "cost-share"],
    "out-of-pocket maximum": ["oop max", "out of pocket limit", "maximum out of pocket", "moop"],
    "prior authorization": ["prior auth", "preauthorization", "pre-auth", "pa", "precertification"],
    "formulary": ["drug formulary", "preferred drug list", "pdl", "drug list"],
    "in-network": ["participating provider", "network provider", "preferred provider", "tier 1"],
    "out-of-network": ["non-participating", "non-network", "non-preferred", "tier 3"],
    "emergency room": ["er", "emergency department", "ed", "emergency care"],

    # CPT code families (expand code ranges to descriptions)
    "office visit": ["e/m", "evaluation and management", "99213", "99214", "99215", "outpatient visit"],
    "preventive care": ["wellness visit", "annual physical", "99386", "99395", "preventive exam"],
}


def _expand_query_with_dictionary(
    query: str,
    max_variants: int = 3,
) -> list[str]:
    """
    Expand a query using the healthcare synonym dictionary.

    Algorithm:
      1. Lowercase the query.
      2. Scan for any dictionary key present as a substring.
      3. For each matched key, generate one variant per synonym by replacing
         the matched key with the synonym in the original query.
      4. Deduplicate and cap at max_variants.

    The original query is always included as the first variant.

    Args:
        query:        Raw user query.
        max_variants: Maximum total variants including the original.

    Returns:
        List of query variants [original, variant_1, variant_2, ...].
    """
    query_lower = query.lower()
    variants: list[str] = [query]
    seen: set[str] = {query_lower}

    for term, synonyms in HEALTHCARE_SYNONYM_DICT.items():
        if len(variants) >= max_variants:
            break
        if term in query_lower:
            for synonym in synonyms:
                if len(variants) >= max_variants:
                    break
                # Replace term with synonym in the original query (case-preserving)
                variant = query_lower.replace(term, synonym)
                if variant not in seen:
                    variants.append(variant)
                    seen.add(variant)

    return variants


async def _expand_query_with_llm(
    query: str,
    max_variants: int = 3,
) -> list[str]:
    """
    Expand a query using LLM for higher-quality contextual synonym generation.

    The LLM understands clinical context and generates more precise expansions
    than dictionary matching. For example, "heart failure" in the context of
    "inpatient admission for heart failure" correctly expands to include
    "heart failure hospitalization" — a phrase unlikely to be in a fixed dict.

    Adds ~400-600ms. Recommended for complex multi-concept clinical queries.

    Returns the original query plus LLM-generated variants.
    """
    from backend.llm.client import get_llm_client

    system = (
        "You are a medical terminology expert and insurance analyst. "
        "Generate alternative phrasings of the given query that would help "
        "find relevant insurance policy documents. Include clinical synonyms, "
        "ICD-10/CPT code contexts, and insurance terminology variants. "
        "Return ONLY a JSON array of strings (the alternative queries). "
        "Include exactly {n} alternatives, not including the original.".format(
            n=max_variants - 1
        )
    )

    from backend.llm.client import get_llm_client
    import json

    llm = get_llm_client()
    try:
        response = await llm.complete(
            system_prompt=system,
            user_message=query,
            temperature=0.3,
            max_tokens=256,
        )
        cleaned = response.strip().strip("```json").strip("```").strip()
        alternatives: list[str] = json.loads(cleaned)
        variants = [query] + [a for a in alternatives if isinstance(a, str)]
        return variants[:max_variants]
    except Exception as exc:
        logger.warning("llm_query_expansion_failed", error=str(exc))
        # Fall back to dictionary expansion
        return _expand_query_with_dictionary(query, max_variants)


async def expand_query(
    query: str,
    backend: str = "dictionary",
    max_variants: int = 3,
) -> list[str]:
    """
    Expand a user query into semantic synonym variants for improved recall.

    Pre-retrieval query expansion is critical in healthcare RAG because:
      • Medical documents use clinical terminology; users use lay terms.
      • BM25 (sparse leg) requires exact term overlap; expansion bridges the gap.
      • A single query for "heart attack coverage" may miss the policy clause
        titled "Myocardial Infarction Benefits" without expansion.

    Args:
        query:        Raw user query string.
        backend:      'dictionary' (zero-latency) or 'llm' (~500ms, higher quality).
        max_variants: Total query variants including original. Typically 3-5.

    Returns:
        List of query strings: [original, variant_1, ..., variant_n].
        Always contains at least the original query.
    """
    start = time.perf_counter()

    if backend == "llm":
        variants = await _expand_query_with_llm(query, max_variants)
    else:
        variants = await asyncio.to_thread(
            _expand_query_with_dictionary, query, max_variants
        )

    elapsed = (time.perf_counter() - start) * 1000
    logger.info(
        "query_expanded",
        original=query[:60],
        num_variants=len(variants),
        backend=backend,
        ms=f"{elapsed:.1f}",
    )
    return variants


# ─────────────────────────────────────────────────────────────────────────────
# Reciprocal Rank Fusion (RRF)
# ─────────────────────────────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    result_lists: list[list[RetrievedChunk]],
    k: int = 60,
    top_n: int = 10,
) -> list[RankedChunk]:
    """
    Fuse multiple ranked result lists using Reciprocal Rank Fusion.

    RRF formula:  score(d) = Σ_i  1 / (k + rank_i(d))

    Where:
      • k = smoothing constant (60 is empirically optimal across IR benchmarks)
      • rank_i(d) = position of document d in list i (1-indexed)
      • If d is absent from list i, it contributes 0 for that list.

    Why k=60?
      The k constant controls the penalty for low-ranked documents.
      At k=60, rank 1 contributes 1/61 ≈ 0.0164, rank 10 contributes
      1/70 ≈ 0.0143, rank 60 contributes 1/120 ≈ 0.0083.
      This relatively gentle decay ensures that a document ranked 15th in one
      list but 1st in another still ranks highly after fusion — which is
      exactly the behavior we want for healthcare RAG where a CPT code may
      rank #1 in BM25 but #15 in dense (because the dense index embeds
      the code description, not the literal code).

    Args:
        result_lists: List of ranked RetrievedChunk lists from multiple sources
                      (dense leg, sparse leg, or multiple query variant results).
        k:            RRF smoothing constant. Default 60.
        top_n:        Number of top results to return after fusion.

    Returns:
        List of RankedChunk sorted by RRF score descending (rank 1 = best).
    """
    # Map chunk_id → {rrf_score, chunk, dense_rank, sparse_rank}
    scores: dict[str, dict[str, Any]] = {}

    for list_idx, result_list in enumerate(result_lists):
        for rank, chunk in enumerate(result_list, start=1):
            if chunk.chunk_id not in scores:
                scores[chunk.chunk_id] = {
                    "rrf_score": 0.0,
                    "chunk": chunk,
                    "dense_rank": None,
                    "sparse_rank": None,
                }
            scores[chunk.chunk_id]["rrf_score"] += 1.0 / (k + rank)

            # Track which leg produced this result
            if chunk.dense_rank is not None:
                scores[chunk.chunk_id]["dense_rank"] = chunk.dense_rank
            if chunk.sparse_rank is not None:
                scores[chunk.chunk_id]["sparse_rank"] = chunk.sparse_rank

    # Sort by RRF score descending
    sorted_items = sorted(
        scores.values(), key=lambda x: x["rrf_score"], reverse=True
    )[:top_n]

    ranked: list[RankedChunk] = []
    for final_rank, item in enumerate(sorted_items, start=1):
        chunk: RetrievedChunk = item["chunk"]
        ranked.append(RankedChunk(
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            metadata=chunk.metadata,
            rrf_score=item["rrf_score"],
            rrf_rank=final_rank,
            dense_rank=item["dense_rank"],
            sparse_rank=item["sparse_rank"],
        ))

    logger.debug(
        "rrf_fusion_complete",
        input_lists=len(result_lists),
        unique_candidates=len(scores),
        top_n=len(ranked),
    )
    return ranked


# ─────────────────────────────────────────────────────────────────────────────
# Cross-Encoder Re-Ranker
# ─────────────────────────────────────────────────────────────────────────────

class CrossEncoderReranker:
    """
    Cross-encoder model for final stage re-ranking of RRF candidates.

    Architecture rationale:
    ────────────────────────
    A bi-encoder (used in the dense retrieval stage) encodes query and
    document SEPARATELY and measures their similarity via dot product.
    Fast, but misses fine-grained interaction signals.

    A cross-encoder encodes [QUERY + SEP + DOCUMENT] JOINTLY, allowing
    full attention between every query token and every document token.
    This is far more accurate at scoring true relevance but ~100x slower —
    so it's only practical for re-ranking a small candidate set (10-20 chunks).

    The two-stage pipeline achieves near-oracle precision on the final
    context window while maintaining sub-second retrieval latency.

    Model: cross-encoder/ms-marco-MiniLM-L-6-v2
      • 22M parameters (MiniLM-L6)
      • Trained on MS MARCO passage retrieval
      • Output: relevance logit (unbounded, but typically -10 to +10)
      • ~50ms for 10 query-chunk pairs on CPU
    """

    _model = None  # Class-level model cache (loaded once)

    @classmethod
    def _get_model(cls, model_name: str):
        if cls._model is None:
            try:
                from sentence_transformers import CrossEncoder
                cls._model = CrossEncoder(model_name)
                logger.info("cross_encoder_loaded", model=model_name)
            except ImportError:
                raise ImportError(
                    "Install sentence-transformers: pip install sentence-transformers"
                )
        return cls._model

    def score_pairs(
        self,
        query: str,
        chunks: list[RankedChunk],
        model_name: str,
    ) -> list[tuple[RankedChunk, float]]:
        """
        Score all (query, chunk) pairs with the cross-encoder.

        Args:
            query:      The user's original query (not expanded variants).
            chunks:     RRF-ranked candidate chunks to re-score.
            model_name: HuggingFace cross-encoder model identifier.

        Returns:
            List of (chunk, cross_encoder_score) tuples.
        """
        model = self._get_model(model_name)
        pairs = [(query, chunk.text) for chunk in chunks]
        scores: list[float] = model.predict(pairs).tolist()
        return list(zip(chunks, scores))


async def rerank_with_cross_encoder(
    query: str,
    candidates: list[RankedChunk],
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    score_threshold: float = 0.0,
    final_top_k: int = 5,
) -> list[RankedChunk]:
    """
    Re-rank RRF candidates using a cross-encoder and prune low-scoring chunks.

    The cross-encoder runs in asyncio.to_thread() because model inference
    is CPU-bound and would block the FastAPI event loop.

    Pruning strategy:
      Chunks with cross-encoder score < score_threshold are removed from
      the context window even if they ranked highly in RRF. This prevents
      the LLM from being distracted by marginally relevant content that
      could introduce hallucinations in healthcare adjudication.

    In a HIPAA-covered clinical decision support context, context precision
    is more important than recall at the LLM stage — the retrieval stage
    should have high recall, the LLM stage should have high precision.

    Args:
        query:           Original user query for cross-encoder.
        candidates:      RRF-ranked candidates (typically 10-20).
        model_name:      Cross-encoder HuggingFace model identifier.
        score_threshold: Prune chunks with score < this value.
        final_top_k:     Maximum chunks to return to the LLM context.

    Returns:
        Re-ranked, pruned list of RankedChunk with cross_encoder_score set.
    """
    if not candidates:
        return []

    reranker = CrossEncoderReranker()

    # Run cross-encoder scoring in thread pool (CPU-bound)
    scored_pairs: list[tuple[RankedChunk, float]] = await asyncio.to_thread(
        reranker.score_pairs, query, candidates, model_name
    )

    # Sort by cross-encoder score descending
    scored_pairs.sort(key=lambda p: p[1], reverse=True)

    # Apply threshold + top-k cap
    final: list[RankedChunk] = []
    for final_rank, (chunk, score) in enumerate(scored_pairs, start=1):
        if score < score_threshold:
            logger.debug(
                "chunk_pruned_by_reranker",
                chunk_id=chunk.chunk_id,
                score=f"{score:.3f}",
                threshold=score_threshold,
            )
            continue
        if final_rank > final_top_k:
            break

        # Build new frozen RankedChunk with cross-encoder score
        final.append(RankedChunk(
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            metadata=chunk.metadata,
            rrf_score=chunk.rrf_score,
            rrf_rank=chunk.rrf_rank,
            cross_encoder_score=score,
            final_rank=final_rank,
            dense_rank=chunk.dense_rank,
            sparse_rank=chunk.sparse_rank,
        ))

    logger.info(
        "reranking_complete",
        candidates_in=len(candidates),
        chunks_out=len(final),
        pruned=len(candidates) - len(final),
    )
    return final


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Query Retrieval Aggregator
# ─────────────────────────────────────────────────────────────────────────────

async def retrieve_with_expansion(
    original_query: str,
    hybrid_search_results: list[HybridSearchResult],
    rrf_k: int = 60,
    rrf_top_n: int = 10,
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    reranker_threshold: float = 0.0,
    final_top_k: int = 5,
) -> RetrievalResult:
    """
    Full retrieval pipeline: RRF fusion across query variants + cross-encoder pruning.

    Accepts multiple HybridSearchResult objects (one per query expansion variant)
    and produces a final ranked set of chunks ready for the LLM context window.

    Pipeline:
      1. Collect all dense_results and sparse_results from each variant's search.
      2. Run RRF across all collected lists simultaneously.
         (Dense results from variant 1, sparse from variant 1,
          dense from variant 2, sparse from variant 2, etc.)
      3. Re-rank RRF top-N with cross-encoder.
      4. Return final chunks with full provenance metadata.

    This multi-list RRF naturally handles the case where a term expanded by
    query variant 2 retrieves a highly relevant chunk that variant 1's
    query would not have found — both BM25 results feed into the same RRF.

    Args:
        original_query:        The user's original unmodified query string.
        hybrid_search_results: One HybridSearchResult per query expansion variant.
        rrf_k:                 RRF smoothing constant.
        rrf_top_n:             Candidates to retain after RRF (before cross-encoder).
        reranker_model:        Cross-encoder model name.
        reranker_threshold:    Minimum cross-encoder score to include in LLM context.
        final_top_k:           Maximum chunks in LLM context window.

    Returns:
        RetrievalResult with provenance, timing, and the final_chunks list.
    """
    start = time.perf_counter()

    # Collect ALL ranked lists (dense + sparse from every variant)
    all_result_lists: list[list[RetrievedChunk]] = []
    for hsr in hybrid_search_results:
        if hsr.dense_results:
            all_result_lists.append(hsr.dense_results)
        if hsr.sparse_results:
            all_result_lists.append(hsr.sparse_results)

    total_candidates = sum(len(lst) for lst in all_result_lists)

    # Stage 1: RRF fusion across all lists
    rrf_start = time.perf_counter()
    rrf_ranked = reciprocal_rank_fusion(all_result_lists, k=rrf_k, top_n=rrf_top_n)
    retrieval_ms = (time.perf_counter() - rrf_start) * 1000

    # Stage 2: Cross-encoder re-ranking + pruning
    rerank_start = time.perf_counter()
    final_chunks = await rerank_with_cross_encoder(
        query=original_query,
        candidates=rrf_ranked,
        model_name=reranker_model,
        score_threshold=reranker_threshold,
        final_top_k=final_top_k,
    )
    rerank_ms = (time.perf_counter() - rerank_start) * 1000
    total_ms = (time.perf_counter() - start) * 1000

    logger.info(
        "retrieval_pipeline_complete",
        query_variants=len(hybrid_search_results),
        total_candidates=total_candidates,
        after_rrf=len(rrf_ranked),
        final_chunks=len(final_chunks),
        total_ms=f"{total_ms:.1f}",
    )

    return RetrievalResult(
        original_query=original_query,
        expanded_queries=[hsr.query_text for hsr in hybrid_search_results],
        final_chunks=final_chunks,
        total_candidates_before_rerank=len(rrf_ranked),
        total_candidates_after_rerank=len(final_chunks),
        cache_hit=False,
        retrieval_latency_ms=retrieval_ms,
        rerank_latency_ms=rerank_ms,
        total_latency_ms=total_ms,
    )
