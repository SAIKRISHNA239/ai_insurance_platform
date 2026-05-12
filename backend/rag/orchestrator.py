"""
backend/rag/orchestrator.py
────────────────────────────
Full ingestion pipeline orchestrator.

Composes the four pipeline stages in the correct order:
  INGEST → SANITIZE → CHUNK → (caller embeds + upserts)

Usage:
    from backend.rag.orchestrator import run_ingestion_pipeline

    result = await run_ingestion_pipeline(
        file_path=Path("/data/policy_acme_2024.pdf"),
        document_id="doc-uuid-here",
        tenant_id="acme-insurance",
        allowed_roles=["admin", "underwriter"],
        document_type=DocumentType.POLICY,
    )

    # result.chunks are ready for embeddings/service.py → vectorstore/service.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from backend.rag.chunking import ChunkingConfig, chunk_elements
from backend.rag.ingestion import ingest_pdf
from backend.rag.sanitization import sanitize_text_async
from backend.rag.schemas import (
    DocumentChunk,
    DocumentType,
    IngestionResult,
    ParsedElement,
    ParsedTableElement,
    ParsedTextElement,
    ParsedTitleElement,
)

logger = structlog.get_logger(__name__)


@dataclass
class PipelineOutput:
    """Return type from the full ingestion pipeline."""
    chunks: list[DocumentChunk]
    result: IngestionResult
    # Sanitization audit logs per element index — store separately from vector DB
    sanitization_audit: dict[int, Any] = field(default_factory=dict)


async def run_ingestion_pipeline(
    file_path: Path,
    document_id: str,
    tenant_id: str,
    allowed_roles: list[str],
    document_type: DocumentType = DocumentType.POLICY,
    sanitizer_backend: str = "regex",
    chunking_config: ChunkingConfig | None = None,
    enrich_tables: bool = True,
    document_effective_date: Any = None,
) -> PipelineOutput:
    """
    Execute the full document ingestion pipeline end-to-end.

    Stage 1: INGEST     — parse PDF with unstructured, enrich tables via LLM
    Stage 2: SANITIZE   — HIPAA PHI redaction on every text element
    Stage 3: CHUNK      — semantic chunking into DocumentChunk DTOs

    Note: Embedding (Stage 4) and VectorStore upsert (Stage 5) are left
    to the caller to allow batching across multiple documents.

    Args:
        file_path:              Path to the PDF file.
        document_id:            Pre-assigned UUID for this document.
        tenant_id:              Tenant namespace for multi-tenant isolation.
        allowed_roles:          RBAC roles that may retrieve chunks from this doc.
        document_type:          Classification of the document.
        sanitizer_backend:      'regex' (default) or 'presidio'.
        chunking_config:        Override default ChunkingConfig.
        enrich_tables:          Whether to LLM-enrich extracted tables.
        document_effective_date: Policy effective date for time-aware retrieval.

    Returns:
        PipelineOutput with chunks, IngestionResult stats, and audit logs.
    """
    overall_start = time.perf_counter()
    errors: list[str] = []
    warnings: list[str] = []

    source_filename = file_path.name

    # ── Stage 1: INGEST ───────────────────────────────────────────────────
    logger.info("pipeline_stage_ingest", document_id=document_id, file=source_filename)
    try:
        elements, parse_stats = await ingest_pdf(
            file_path=file_path,
            document_type=document_type,
            enrich_tables=enrich_tables,
        )
    except Exception as exc:
        logger.exception("pipeline_ingest_failed", error=str(exc))
        errors.append(f"Ingestion failed: {exc}")
        return PipelineOutput(
            chunks=[],
            result=IngestionResult(
                document_id=document_id,
                source_filename=source_filename,
                document_type=document_type,
                tenant_id=tenant_id,
                total_parsed_elements=0,
                table_elements_found=0,
                table_elements_enriched=0,
                total_chunks_produced=0,
                total_chunks_upserted=0,
                total_phi_redactions=0,
                phi_entity_breakdown={},
                parse_time_ms=0,
                sanitization_time_ms=0,
                chunking_time_ms=0,
                embedding_time_ms=0,
                total_time_ms=(time.perf_counter() - overall_start) * 1000,
                succeeded=False,
                errors=errors,
            ),
        )

    # ── Stage 2: SANITIZE ─────────────────────────────────────────────────
    logger.info("pipeline_stage_sanitize", document_id=document_id, element_count=len(elements))
    sanitize_start = time.perf_counter()
    phi_redaction_counts: dict[int, int] = {}
    sanitization_audit: dict[int, Any] = {}
    total_phi_redactions = 0
    combined_phi_summary: dict[str, int] = {}

    sanitized_elements: list[ParsedElement] = []

    for idx, element in enumerate(elements):
        if isinstance(element, ParsedTableElement):
            # Sanitize both raw_text and enriched content
            raw_result = await sanitize_text_async(element.raw_text, sanitizer_backend)
            enriched_result = None
            if element.enriched_markdown:
                enriched_result = await sanitize_text_async(
                    element.enriched_markdown, sanitizer_backend
                )
            if element.llm_summary:
                summary_result = await sanitize_text_async(
                    element.llm_summary, sanitizer_backend
                )
            else:
                summary_result = raw_result

            combined_redaction_count = (
                raw_result.redaction_count
                + (enriched_result.redaction_count if enriched_result else 0)
                + (summary_result.redaction_count if summary_result else 0)
            )

            sanitized_elements.append(
                ParsedTableElement(
                    element_type="table",
                    raw_html=element.raw_html,
                    raw_text=raw_result.sanitized_text,
                    page_number=element.page_number,
                    row_count=element.row_count,
                    col_count=element.col_count,
                    enriched_markdown=enriched_result.sanitized_text if enriched_result else element.enriched_markdown,
                    llm_summary=summary_result.sanitized_text if summary_result else element.llm_summary,
                )
            )
            phi_redaction_counts[idx] = combined_redaction_count
            sanitization_audit[idx] = raw_result.redactions

        elif isinstance(element, ParsedTextElement):
            san_result = await sanitize_text_async(element.text, sanitizer_backend)
            sanitized_elements.append(
                ParsedTextElement(
                    text=san_result.sanitized_text,
                    page_number=element.page_number,
                )
            )
            phi_redaction_counts[idx] = san_result.redaction_count
            sanitization_audit[idx] = san_result.redactions

            for entity, count in san_result.phi_entity_summary.items():
                combined_phi_summary[entity] = combined_phi_summary.get(entity, 0) + count
            total_phi_redactions += san_result.redaction_count

        elif isinstance(element, ParsedTitleElement):
            san_result = await sanitize_text_async(element.text, sanitizer_backend)
            sanitized_elements.append(
                ParsedTitleElement(
                    text=san_result.sanitized_text,
                    heading_level=element.heading_level,
                    page_number=element.page_number,
                )
            )
            phi_redaction_counts[idx] = san_result.redaction_count

    sanitize_time_ms = (time.perf_counter() - sanitize_start) * 1000

    # ── Stage 3: CHUNK ────────────────────────────────────────────────────
    logger.info("pipeline_stage_chunk", document_id=document_id)
    chunk_start = time.perf_counter()
    chunks = await chunk_elements(
        elements=sanitized_elements,
        document_id=document_id,
        tenant_id=tenant_id,
        source_filename=source_filename,
        document_type=document_type,
        allowed_roles=allowed_roles,
        config=chunking_config,
        phi_redaction_counts=phi_redaction_counts,
        document_effective_date=document_effective_date,
    )
    chunk_time_ms = (time.perf_counter() - chunk_start) * 1000

    total_ms = (time.perf_counter() - overall_start) * 1000

    result = IngestionResult(
        document_id=document_id,
        source_filename=source_filename,
        document_type=document_type,
        tenant_id=tenant_id,
        total_parsed_elements=len(elements),
        table_elements_found=parse_stats.get("table_count", 0),
        table_elements_enriched=parse_stats.get("tables_enriched", 0),
        total_chunks_produced=len(chunks),
        total_chunks_upserted=0,  # Caller handles upsert
        total_phi_redactions=total_phi_redactions,
        phi_entity_breakdown=combined_phi_summary,
        parse_time_ms=parse_stats.get("parse_time_ms", 0),
        sanitization_time_ms=sanitize_time_ms,
        chunking_time_ms=chunk_time_ms,
        embedding_time_ms=0,  # Caller handles embedding
        total_time_ms=total_ms,
        succeeded=True,
        errors=errors,
        warnings=warnings,
    )

    logger.info(
        "pipeline_complete",
        document_id=document_id,
        chunks=len(chunks),
        phi_redactions=total_phi_redactions,
        total_ms=f"{total_ms:.0f}",
    )

    return PipelineOutput(
        chunks=chunks,
        result=result,
        sanitization_audit=sanitization_audit,
    )
