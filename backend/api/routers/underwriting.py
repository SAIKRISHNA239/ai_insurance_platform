"""
backend/api/routers/underwriting.py
─────────────────────────────────────────────────────────────────────────────
Underwriting AI streaming endpoint.

Endpoint:
  GET  /underwriting/{app_id}/ai-summary/stream  — SSE stream of GenAI analysis

Stream format (newline-delimited, SSE):
  data: {"type": "token",     "content": "...text chunk..."}
  data: {"type": "citations", "citations": [...]}
  data: {"type": "done"}
  data: {"type": "error",     "error": "message"}

The Gemini prompt constructs a medical underwriting summary from the
application's health questionnaire data.  In a production system this
would also run a RAG query against ChromaDB to retrieve relevant policy
and medical evidence documents and inject them as citations.
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
from backend.database.models import Application, User
from backend.llm.client import GeminiClient, get_llm_client

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/underwriting", tags=["Underwriting AI"])


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


async def _stream_gemini(app: Application):
    """
    Async generator yielding SSE-formatted strings.
    Falls back to DB notes if Gemini fails.
    """
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

        # Ensure it's a GeminiClient (the only one with stream_complete)
        if not isinstance(client, GeminiClient):
            raise RuntimeError(f"LLM provider '{type(client).__name__}' does not support streaming. Set LLM_PROVIDER=gemini in .env.")

        accumulated = ""
        async for token in client.stream_complete(
            system_prompt=UNDERWRITING_SYSTEM_PROMPT,
            user_message=user_message,
        ):
            accumulated += token
            yield _sse({"type": "token", "content": token})

        # After streaming completes, emit a citations frame
        # (In production, this would be populated from RAG retrieval)
        yield _sse({"type": "citations", "citations": []})
        yield _sse({"type": "done"})

        logger.info(
            "underwriting_ai_summary_generated",
            app_id=str(app.id),
            tokens=len(accumulated),
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
      {"type": "citations", "citations": [...]}  ← source documents
      {"type": "done"}                           ← stream complete
      {"type": "error",     "error": "..."}      ← error occurred
    """
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()

    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    return StreamingResponse(
        _stream_gemini(app),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        },
    )
