"""
backend/rag/pipeline.py
────────────────────────
RAG (Retrieval-Augmented Generation) pipeline for healthcare insurance.

Pipeline stages:
  1. RETRIEVE — embed query, search vectorstore for relevant chunks
  2. AUGMENT  — format retrieved chunks into a structured context prompt
  3. GENERATE — call LLM with context + user query

Use cases:
  • Claims adjudication: "Is this procedure covered under policy X?"
  • Underwriting guidance: "What are the exclusions for pre-existing conditions?"
  • Fraud reasoning: "Does this claim pattern match known fraud signatures?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from backend.embeddings.service import embed_text
from backend.llm.client import get_llm_client
from backend.vectorstore.service import query_similar

logger = structlog.get_logger(__name__)

# Default system prompt for insurance RAG queries
INSURANCE_SYSTEM_PROMPT = """You are an expert healthcare insurance analyst with deep knowledge of:
- CPT/HCPCS procedure codes and their clinical significance
- ICD-10-CM diagnosis codes and comorbidity patterns
- Insurance policy coverage rules and exclusion clauses
- EDI 837 claim formats and adjudication standards
- HIPAA compliance requirements

Use the provided context documents to answer the question accurately.
If the context is insufficient to answer definitively, say so clearly.
Always cite which context document supports your answer.
"""


@dataclass
class RAGResult:
    """Structured result from a RAG pipeline invocation."""
    answer: str
    retrieved_chunks: list[dict[str, Any]] = field(default_factory=list)
    collection_searched: str = ""
    query_used: str = ""
    token_context_size: int = 0


async def run_rag_query(
    query: str,
    collection_name: str,
    n_retrieve: int = 5,
    where_filter: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    temperature: float = 0.1,
) -> RAGResult:
    """
    Execute a full RAG pipeline query.

    Args:
        query: The natural language question to answer.
        collection_name: ChromaDB collection to search.
        n_retrieve: Number of context chunks to retrieve.
        where_filter: Optional metadata filter for ChromaDB query.
        system_prompt: Override the default insurance system prompt.
        temperature: LLM generation temperature.

    Returns:
        RAGResult with the generated answer and retrieved source chunks.
    """
    logger.info("rag_pipeline_started", query_preview=query[:80], collection=collection_name)

    # ── Stage 1: RETRIEVE ─────────────────────────────────────────────────
    query_embedding = await embed_text(query)
    chunks = await query_similar(
        collection_name=collection_name,
        query_embedding=query_embedding,
        n_results=n_retrieve,
        where=where_filter,
    )

    logger.debug("rag_retrieved", n_chunks=len(chunks))

    # ── Stage 2: AUGMENT ──────────────────────────────────────────────────
    if not chunks:
        context_text = "No relevant documents found in the knowledge base."
    else:
        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            meta_str = ", ".join(f"{k}={v}" for k, v in chunk["metadata"].items())
            context_parts.append(
                f"[Document {i}] (similarity: {1 - chunk['distance']:.3f}, {meta_str})\n"
                f"{chunk['document']}"
            )
        context_text = "\n\n---\n\n".join(context_parts)

    augmented_prompt = (
        f"CONTEXT DOCUMENTS:\n{context_text}\n\n"
        f"QUESTION: {query}\n\n"
        f"Please answer based on the context above."
    )

    # ── Stage 3: GENERATE ─────────────────────────────────────────────────
    llm = get_llm_client()
    answer = await llm.complete(
        system_prompt=system_prompt or INSURANCE_SYSTEM_PROMPT,
        user_message=augmented_prompt,
        temperature=temperature,
    )

    logger.info("rag_pipeline_complete", answer_preview=answer[:80])

    return RAGResult(
        answer=answer,
        retrieved_chunks=chunks,
        collection_searched=collection_name,
        query_used=query,
        token_context_size=len(augmented_prompt),
    )
