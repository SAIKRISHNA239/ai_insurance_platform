"""
backend/api/routers/knowledge.py
──────────────────────────────────
Knowledge Base (RAG) ingestion endpoint.

Endpoints:
  POST /knowledge/upload — upload a PDF and index it into the vector store

Pipeline (background task):
  1. Save the upload to a temp file
  2. run_ingestion_pipeline() → parse → sanitize → chunk → PipelineOutput
  3. Embed each chunk via LLM client
  4. Upsert chunks into the policy_vectors ChromaDB collection
  5. Clean up temp file

Auth: ADMIN or UNDERWRITER only.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel

from backend.api.deps import get_current_user, require_role
from backend.config import get_settings
from backend.database.models import User, UserRole
from backend.rag.schemas import DocumentType

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/knowledge", tags=["Knowledge Base"])

_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/octet-stream",  # Some browsers send PDFs as this
}
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


# ── Response schema ───────────────────────────────────────────────────────────

class KnowledgeUploadResponse(BaseModel):
    document_id: str
    filename: str
    message: str
    status: str


# ── Background ingestion task ─────────────────────────────────────────────────

async def _run_ingestion(
    tmp_path: Path,
    document_id: str,
    filename: str,
    tenant_id: str,
    user_role: str,
) -> None:
    """
    Full ingestion pipeline run in a background task so the HTTP response
    returns immediately (202 Accepted pattern).

    Steps:
      1. run_ingestion_pipeline() → PipelineOutput (parse, sanitize, chunk)
      2. For each chunk, embed with LLM and upsert to ChromaDB.
      3. Delete the temp file.
    """
    from backend.rag.orchestrator import run_ingestion_pipeline
    from backend.rag.schemas import DocumentType
    from backend.llm.client import get_llm_client
    from backend.vectorstore.client import upsert_chunk

    settings = get_settings()
    collection = settings.chroma_collection_policies

    try:
        output = await run_ingestion_pipeline(
            file_path=tmp_path,
            document_id=document_id,
            tenant_id=tenant_id,
            allowed_roles=["admin", "underwriter", "claims_adjuster"],
            document_type=DocumentType.POLICY,
            sanitizer_backend="regex",
            enrich_tables=False,  # Keep fast; enable in prod config
        )

        if not output.result.succeeded:
            logger.error(
                "knowledge_ingestion_failed",
                document_id=document_id,
                errors=output.result.errors,
            )
            return

        llm = get_llm_client()
        embedded = 0
        for chunk in output.chunks:
            try:
                embedding = await llm.embed(chunk.text)
                await upsert_chunk(
                    collection_name=collection,
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    embedding=embedding,
                    chunk_metadata=chunk.metadata,
                    allowed_roles=["admin", "underwriter", "claims_adjuster"],
                )
                embedded += 1
            except Exception as embed_exc:
                logger.warning(
                    "chunk_embed_failed",
                    chunk_id=chunk.chunk_id,
                    error=str(embed_exc),
                )

        logger.info(
            "knowledge_ingestion_complete",
            document_id=document_id,
            filename=filename,
            chunks_total=len(output.chunks),
            chunks_embedded=embedded,
            phi_redactions=output.result.total_phi_redactions,
        )

    except Exception as exc:
        logger.exception(
            "knowledge_ingestion_unhandled_error",
            document_id=document_id,
            error=str(exc),
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post(
    "/upload",
    response_model=KnowledgeUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a PDF into the RAG knowledge base (admin/underwriter)",
    dependencies=[Depends(require_role(UserRole.ADMIN, UserRole.UNDERWRITER))],
)
async def upload_knowledge_document(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
) -> KnowledgeUploadResponse:
    """
    Accept a PDF upload and queue it for full RAG ingestion.

    Returns 202 Accepted immediately. The ingestion pipeline
    (parse → sanitize → chunk → embed → upsert) runs as a background task.

    Use GET /knowledge/status/{document_id} (future endpoint) to check progress.
    """
    # Validate content type
    content_type = file.content_type or ""
    filename = file.filename or "document.pdf"

    if not filename.lower().endswith(".pdf") and content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF files are accepted for knowledge base ingestion.",
        )

    # Read and size-check
    raw = await file.read()
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the 50 MB limit ({len(raw) / 1024 / 1024:.1f} MB uploaded).",
        )
    if len(raw) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    # Write to a named temp file (pipeline needs a real Path)
    document_id = str(uuid.uuid4())
    suffix = Path(filename).suffix or ".pdf"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    logger.info(
        "knowledge_upload_received",
        document_id=document_id,
        filename=filename,
        size_bytes=len(raw),
        user_id=str(current_user.id),
    )

    # Queue background ingestion
    background_tasks.add_task(
        _run_ingestion,
        tmp_path=tmp_path,
        document_id=document_id,
        filename=filename,
        tenant_id=str(current_user.tenant_id),
        user_role=current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role),
    )

    return KnowledgeUploadResponse(
        document_id=document_id,
        filename=filename,
        message=(
            "Document accepted and queued for ingestion. "
            "Chunks will be available in the vector store within 30–120 seconds."
        ),
        status="processing",
    )
