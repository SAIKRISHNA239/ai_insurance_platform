"""
backend/api/routers/underwriting.py
─────────────────────────────────────────────────────────────────────────────
Underwriting AI streaming endpoint with live RAG citations.

Endpoint:
  GET  /underwriting/{app_id}/ai-summary/stream  — SSE stream of GenAI analysis

Stream format (newline-delimited, SSE):
  data: {"type": "token",     "content": "...text chunk..."}
  data: {"type": "citations", "citations": [...]}
  data: {"type": "done"}
  data: {"type": "error",     "error": "message"}

RAG PIPELINE (live — Task 7)
─────────────────────────────
Before the LLM generates its summary the endpoint now runs a full retrieval
pipeline against the ChromaDB policy_vectors collection:

  1. Build a clinical query from the application's health questionnaire data.
  2. Expand the query with the healthcare synonym dictionary (zero-latency).
  3. Run hybrid_search() (dense ANN + BM25 sparse) for each query variant.
  4. Fuse results with Reciprocal Rank Fusion (RRF).
  5. Re-rank top candidates with a cross-encoder for precision.
  6. Format retrieved RankedChunks as frontend Citation objects.

The citations frame is emitted immediately after the token stream ends so
the UI can render source documents without waiting for LLM generation.

GRACEFUL DEGRADATION
─────────────────────
If ChromaDB is unreachable (e.g., Docker not started) or the collection is
empty, the RAG stage raises and is caught silently — the LLM summary is still
streamed and citations: [] is emitted.  This prevents vector DB downtime from
breaking the underwriting workflow.
"""

from __future__ import annotations

import json
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user, get_db
from backend.config import get_settings
from backend.database.models import Application, User
from backend.llm.client import GeminiClient, get_llm_client
from backend.rag.retriever import (
    RankedChunk,
    RetrievalResult,
    expand_query,
    reciprocal_rank_fusion,
    rerank_with_cross_encoder,
)
from backend.vectorstore.client import hybrid_search

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/underwriting", tags=["Underwriting AI"])

# Collection name from settings (default: "policy_vectors")
_POLICY_COLLECTION = get_settings().chroma_collection_policies


# ─── System prompt ────────────────────────────────────────────────────────────

UNDERWRITING_SYSTEM_PROMPT = """You are a senior medical underwriter AI assistant for an enterprise health insurance platform.

Your task: Given an insurance application's data, generate a structured clinical underwriting summary.

Format your response as a professional narrative (2-4 paragraphs) covering:
1. Risk Assessment: overall risk profile, key medical findings
2. Actuarial Impact: how conditions affect insurability and premium
3. Recommendation: suggested risk tier (Preferred / Standard / Substandard / Decline), table rating, and any exclusion riders

Guidelines:
- Be concise, clinical, and evidence-based
- Reference specific health data from the application
- Use medical terminology appropriately
- Do NOT fabricate diagnoses not present in the data
- End with a clear routing recommendation
"""


def _sse(payload: dict) -> str:
    """Format a dict as a Server-Sent Events data line."""
    return f"data: {json.dumps(payload)}\n\n"


# ─── RAG Retrieval ────────────────────────────────────────────────────────────

def _build_rag_query(app: Application) -> str:
    """
    Build a clinical RAG query from the application's health data.

    Combines policy type, coverage amount, and health questionnaire boolean
    flags into a single natural-language query that will match relevant policy
    clauses and underwriting guidelines in the vector store.
    """
    hq: dict = app.health_questionnaire or {}

    # Translate boolean flags to clinical phrases
    condition_phrases: list[str] = []
    flag_map = {
        "smoker":                  "tobacco use nicotine",
        "pre_existing_conditions": "pre-existing conditions chronic illness",
        "recent_surgery":          "recent surgery hospitalization",
        "family_history_heart":    "family history heart disease cardiovascular",
        "current_medications":     "prescription medication drug therapy",
    }
    for key, phrase in flag_map.items():
        if hq.get(key):
            condition_phrases.append(phrase)

    policy_type = app.policy_type.value if app.policy_type else "individual"
    coverage    = f"${app.requested_coverage_limit:,.0f}" if app.requested_coverage_limit else "standard"

    base_query = (
        f"Underwriting guidelines and coverage rules for {policy_type} health insurance "
        f"with coverage limit {coverage}"
    )

    if condition_phrases:
        conditions_str = ", ".join(condition_phrases)
        base_query += f". Applicant has: {conditions_str}."
    else:
        base_query += ". Standard risk applicant with no significant conditions."

    return base_query


def _format_citation(chunk: RankedChunk, citation_idx: int) -> dict:
    """
    Map a RankedChunk from the RAG pipeline to the frontend Citation interface.

    Frontend Citation shape (from underwriting/page.tsx):
      id            — unique citation reference number (1-based)
      chunk_id      — internal vector DB chunk identifier
      document_name — human-readable source document name
      document_url  — URL to the original document (or empty string if unavailable)
      bounding_box  — page/coordinate metadata (null if not available)
      excerpt       — the retrieved text snippet shown in the citations panel
    """
    meta = chunk.metadata or {}

    # Extract document name from metadata — fall back to chunk_id prefix
    doc_name = (
        meta.get("source_filename")
        or meta.get("document_name")
        or meta.get("document_id", "")[:8]
        or f"Policy Document {citation_idx}"
    )

    # Build a document URL if a storage path is present
    doc_url = meta.get("document_url") or meta.get("storage_path") or ""

    # Bounding box from metadata (page/coordinates stored at ingest time)
    bounding_box = None
    if meta.get("page_number") is not None:
        bounding_box = {
            "page": int(meta["page_number"]),
            "x": float(meta.get("bbox_x", 0.0)),
            "y": float(meta.get("bbox_y", 0.0)),
            "width": float(meta.get("bbox_w", 1.0)),
            "height": float(meta.get("bbox_h", 0.05)),
        }

    # Truncate excerpt to 400 chars — enough context without bloating the SSE frame
    excerpt = chunk.text[:400].strip()
    if len(chunk.text) > 400:
        excerpt += "…"

    return {
        "id": citation_idx,
        "chunk_id": chunk.chunk_id,
        "document_name": doc_name,
        "document_url": doc_url,
        "bounding_box": bounding_box,
        "excerpt": excerpt,
        "rrf_score": round(chunk.rrf_score, 4),
        "cross_encoder_score": (
            round(chunk.cross_encoder_score, 4)
            if chunk.cross_encoder_score is not None
            else None
        ),
        "final_rank": chunk.final_rank,
    }


async def _retrieve_citations(app: Application, current_user: User) -> list[dict]:
    """
    Run the full RAG retrieval pipeline and return a list of formatted citations.

    Pipeline:
      1. Build clinical query from application health data.
      2. Expand query with healthcare synonym dictionary.
      3. Embed each query variant with the LLM client.
      4. Run hybrid_search() (dense + BM25) for each variant.
      5. Fuse via RRF, re-rank with cross-encoder.
      6. Format top-k chunks as frontend Citation dicts.

    Returns [] gracefully on any failure (ChromaDB down, empty collection, etc.)
    so the LLM stream is never blocked by retrieval errors.
    """
    query = _build_rag_query(app)
    logger.info(
        "rag_retrieval_started",
        app_id=str(app.id),
        query_preview=query[:80],
    )

    # Step 1: Query expansion (dictionary backend — zero latency)
    expanded_queries = await expand_query(query, backend="dictionary", max_variants=3)

    # Step 2: Embed all variants + hybrid search concurrently
    llm = get_llm_client()
    tenant_id = str(current_user.tenant_id)
    user_role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    import asyncio

    async def _search_variant(q: str):
        embedding = await llm.embed(q)
        return await hybrid_search(
            collection_name=_POLICY_COLLECTION,
            query_text=q,
            query_embedding=embedding,
            tenant_id=tenant_id,
            user_role=user_role,
            dense_top_k=10,
            sparse_top_k=10,
        )

    hybrid_results = await asyncio.gather(
        *[_search_variant(q) for q in expanded_queries],
        return_exceptions=True,
    )

    # Filter out any failed variants (e.g., empty collection for a specific role)
    valid_results = [r for r in hybrid_results if not isinstance(r, Exception)]

    if not valid_results:
        logger.warning(
            "rag_retrieval_empty",
            app_id=str(app.id),
            collection=_POLICY_COLLECTION,
        )
        return []

    # Step 3: RRF fusion across all valid variant results
    all_lists = []
    for hsr in valid_results:
        if hsr.dense_results:
            all_lists.append(hsr.dense_results)
        if hsr.sparse_results:
            all_lists.append(hsr.sparse_results)

    if not all_lists:
        return []

    rrf_ranked = reciprocal_rank_fusion(all_lists, k=60, top_n=10)

    # Step 4: Cross-encoder re-ranking (prune low-relevance chunks)
    final_chunks = await rerank_with_cross_encoder(
        query=query,
        candidates=rrf_ranked,
        score_threshold=0.0,
        final_top_k=5,
    )

    # Step 5: Format as frontend Citation objects
    citations = [
        _format_citation(chunk, idx + 1)
        for idx, chunk in enumerate(final_chunks)
    ]

    logger.info(
        "rag_retrieval_complete",
        app_id=str(app.id),
        citations=len(citations),
        collection=_POLICY_COLLECTION,
    )
    return citations


# ─── SSE Generator ────────────────────────────────────────────────────────────

async def _stream_gemini(app: Application, current_user: User):
    """
    Async generator yielding SSE-formatted strings.

    Order of frames:
      1. RAG retrieval runs CONCURRENTLY with the LLM stream setup.
         Because Gemini streaming is synchronous under the hood (thread-based),
         we start retrieval as a Task before the first token is yielded and
         await it immediately after the streaming loop completes.
      2. token frames — streamed in real time as Gemini generates
      3. citations frame — emitted once after all tokens, with real RAG results
      4. done frame
    """
    import asyncio

    # Build the user message from application data
    hq = app.health_questionnaire or {}
    user_message = f"""
Application Number: {app.application_number}
Policy Type: {app.policy_type.value}
Coverage Requested: ${app.requested_coverage_limit:,.0f}
Underwriting Score: {app.underwriting_score or 'N/A'}
Risk Tier (preliminary): {app.risk_tier.value if app.risk_tier else 'Not assigned'}

Health Questionnaire:
{json.dumps(hq, indent=2) if hq else "No health questionnaire data provided."}

Existing AI Notes (from initial intake):
{app.ai_underwriting_notes or "None"}

Please generate a full clinical underwriting summary for this application.
"""

    try:
        client = get_llm_client()

        if not isinstance(client, GeminiClient):
            raise RuntimeError(
                f"LLM provider '{type(client).__name__}' does not support streaming. "
                "Set LLM_PROVIDER=gemini in .env."
            )

        # Launch RAG retrieval as a concurrent background task.
        # It runs while Gemini is generating tokens, minimising total latency.
        rag_task: asyncio.Task[list[dict]] = asyncio.ensure_future(
            _retrieve_citations(app, current_user)
        )

        # Stream LLM tokens
        accumulated = ""
        async for token in client.stream_complete(
            system_prompt=UNDERWRITING_SYSTEM_PROMPT,
            user_message=user_message,
        ):
            accumulated += token
            yield _sse({"type": "token", "content": token})

        # Await RAG results — should already be done by now (parallel execution)
        try:
            citations = await rag_task
        except Exception as rag_exc:
            logger.warning(
                "rag_task_failed_graceful_fallback",
                app_id=str(app.id),
                error=str(rag_exc),
            )
            citations = []

        # Emit citations + done
        yield _sse({"type": "citations", "citations": citations})
        yield _sse({"type": "done"})

        logger.info(
            "underwriting_ai_summary_generated",
            app_id=str(app.id),
            tokens=len(accumulated),
            citations=len(citations),
        )

    except Exception as exc:
        logger.warning("underwriting_stream_failed", error=str(exc), app_id=str(app.id))
        yield _sse({"type": "error", "error": str(exc)})


# ─── Route ────────────────────────────────────────────────────────────────────

@router.get(
    "/{app_id}/ai-summary/stream",
    summary="Stream AI underwriting summary (SSE)",
    response_class=StreamingResponse,
)
async def stream_underwriting_summary(
    app_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Server-Sent Events stream of the GenAI underwriting analysis for a given application.
    The client receives JSON objects line by line:
      {"type": "token",     "content": "..."}   ← text chunk
      {"type": "citations", "citations": [...]}  ← source documents (live RAG)
      {"type": "done"}                           ← stream complete
      {"type": "error",     "error": "..."}      ← error occurred
    """
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()

    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    return StreamingResponse(
        _stream_gemini(app, current_user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        },
    )
