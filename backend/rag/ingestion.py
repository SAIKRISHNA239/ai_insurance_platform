"""
backend/rag/ingestion.py
─────────────────────────
PDF ingestion pipeline using the `unstructured` library with LLM-powered
table enrichment.

Architectural Overview
──────────────────────
The ingestion pipeline is responsible for the first two stages of RAG:
  LOAD → PARSE → ENRICH → (hand off to chunking.py)

It is explicitly NOT responsible for:
  • Chunking (→ chunking.py)
  • Embedding (→ embeddings/service.py)
  • Vector upsert (→ vectorstore/service.py)

This separation enforces the Single Responsibility Principle and makes
each stage independently testable and replaceable.

Why `unstructured` with strategy="hi_res"?
───────────────────────────────────────────
Insurance policy PDFs are among the most complex document formats:
  • Multi-column layouts with benefit schedules
  • Embedded benefit tables (deductible grids, co-pay schedules)
  • Mixed fonts (legal fine print vs. benefit summaries)
  • Headers/footers that should NOT be included in chunks
  • Page numbers and watermarks polluting extracted text

`unstructured` with strategy="hi_res" uses a document layout analysis
model (detectron2 or similar) to identify structural regions BEFORE
extracting text, giving far superior results over naive PDF text extraction
(pdfminer, PyMuPDF) for complex layouts.

chunking_strategy="by_title":
  Unstructured groups elements under the nearest preceding title element.
  This creates natural semantic sections: "Coverage Limits" → all benefit
  rows, "Exclusions" → all exclusion paragraphs. This structure is then
  passed to our semantic chunker as pre-formed blocks.

Table Enrichment via LLM
─────────────────────────
Raw HTML tables from PDFs are notoriously noisy (merged cells, colspan
artifacts, garbled text from OCR). We pipe each extracted table through
the LLM to:
  1. Convert it to clean, structured Markdown
  2. Generate a 1-2 sentence natural language summary

This enrichment serves two purposes:
  a) Embedding quality: embedding a Markdown table + prose summary produces
     a richer semantic vector than embedding raw HTML with whitespace noise.
  b) Retrieval context: when a chunk is retrieved, the LLM sees clean
     Markdown + summary, not `<td colspan="3">` garbage.

The enrichment prompt is deliberately deterministic (temperature=0.0) to
ensure reproducible Markdown output for the same input table.

Async Strategy
───────────────
`unstructured.partition.pdf` is synchronous and CPU/IO bound (it may
invoke OCR tools). We run it in `asyncio.to_thread()` to avoid blocking
the FastAPI event loop. Table enrichment LLM calls are async-native and
run concurrently via `asyncio.gather()`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

import structlog

from backend.llm.client import get_llm_client
from backend.rag.schemas import (
    DocumentType,
    ParsedElement,
    ParsedTableElement,
    ParsedTextElement,
    ParsedTitleElement,
)

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# LLM Prompts for Table Enrichment
# ─────────────────────────────────────────────────────────────────────────────

_TABLE_ENRICHMENT_SYSTEM = """You are an expert healthcare insurance document analyst.
You will receive raw HTML table data extracted from an insurance policy PDF.
Your task is to:
1. Convert the table to clean, well-formatted Markdown (use | pipe syntax).
2. Write a 1-2 sentence natural language summary describing what the table represents.

Output your response in this EXACT JSON format (no markdown code fences):
{
  "markdown_table": "<your markdown table here>",
  "summary": "<your 1-2 sentence summary here>"
}

Rules:
- Preserve all numeric values exactly as they appear (do not round or estimate).
- If a cell is empty or illegible, use "N/A".
- The summary must mention the table's purpose and key metrics (e.g., "This table shows the annual deductible amounts for individual and family plans across three tiers").
- Do NOT include any text outside the JSON object.
"""

_TABLE_ENRICHMENT_USER_TEMPLATE = """Raw HTML table from insurance policy document:

{raw_html}

Plain text fallback (from OCR):
{raw_text}

Convert to Markdown and summarize."""


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _unstructured_element_to_dto(
    element: Any,
) -> ParsedElement | None:
    """
    Convert a single `unstructured` Element object to our typed DTO.

    Unstructured returns objects like `Title`, `NarrativeText`, `Table`,
    `ListItem`, `Header`, `Footer`. We map these to our discriminated union.

    Returns None for elements that should be discarded (headers, footers,
    page numbers) to keep the pipeline output clean.
    """
    # Lazy import — unstructured is an optional heavy dependency
    from unstructured.documents.elements import (
        Footer,
        Header,
        NarrativeText,
        PageBreak,
        Table,
        Title,
        ListItem,
        Text,
    )

    # Discard navigation noise
    if isinstance(element, (Header, Footer, PageBreak)):
        return None

    text_content = str(element).strip()
    if not text_content:
        return None

    # Extract page number from element metadata if available
    page_number: int | None = None
    if hasattr(element, "metadata") and element.metadata:
        page_number = getattr(element.metadata, "page_number", None)

    if isinstance(element, Title):
        # Infer heading level from font size metadata (unstructured exposes this
        # in hi_res mode via element.metadata.emphasized_text_tags)
        heading_level = 1
        if hasattr(element, "metadata") and element.metadata:
            tags = getattr(element.metadata, "emphasized_text_tags", None) or []
            if "h2" in tags:
                heading_level = 2
            elif "h3" in tags:
                heading_level = 3

        return ParsedTitleElement(
            text=text_content,
            heading_level=heading_level,
            page_number=page_number,
        )

    if isinstance(element, Table):
        # Unstructured exposes HTML table via element.metadata.text_as_html
        raw_html = ""
        if hasattr(element, "metadata") and element.metadata:
            raw_html = getattr(element.metadata, "text_as_html", "") or ""

        # Count rows/cols from HTML heuristically
        row_count = raw_html.count("<tr")
        col_count = max(raw_html.count("<td"), raw_html.count("<th")) // max(row_count, 1)

        return ParsedTableElement(
            raw_html=raw_html or text_content,
            raw_text=text_content,
            page_number=page_number,
            row_count=row_count,
            col_count=col_count,
        )

    if isinstance(element, (NarrativeText, ListItem, Text)):
        return ParsedTextElement(
            text=text_content,
            page_number=page_number,
        )

    # Fallback: treat as plain text
    return ParsedTextElement(text=text_content, page_number=page_number)


def _parse_pdf_sync(
    file_path: Path,
    strategy: str = "hi_res",
    chunking_strategy: str = "by_title",
    max_characters: int = 4000,
    new_after_n_chars: int = 3000,
    combine_under_n_chars: int = 500,
) -> list[Any]:
    """
    Synchronous wrapper around unstructured's partition_pdf.

    Parameters mirror unstructured's API. Must be called via asyncio.to_thread.

    Key parameters:
        strategy="hi_res":
            Uses layout-detection model to identify structural regions.
            Requires: pip install "unstructured[pdf]" detectron2 tesseract

        chunking_strategy="by_title":
            Groups elements under their nearest title ancestor.
            Prevents mis-merging content from different policy sections.

        max_characters:
            Hard cap on chunk size from unstructured's own chunker.
            Our semantic chunker applies additional splitting downstream.

        combine_under_n_chars:
            Merges tiny fragments (e.g., single-line footnotes) with preceding
            elements to avoid embedding near-empty chunks.
    """
    from unstructured.partition.pdf import partition_pdf

    logger.info(
        "pdf_parsing_started",
        file=file_path.name,
        strategy=strategy,
        chunking_strategy=chunking_strategy,
    )

    elements = partition_pdf(
        filename=str(file_path),
        strategy=strategy,
        chunking_strategy=chunking_strategy,
        max_characters=max_characters,
        new_after_n_chars=new_after_n_chars,
        combine_under_n_chars=combine_under_n_chars,
        # Include page number in metadata for chunk provenance
        include_page_breaks=True,
        # Extract table HTML — critical for LLM enrichment
        infer_table_structure=True,
    )

    logger.info("pdf_parsing_complete", file=file_path.name, element_count=len(elements))
    return elements


# ─────────────────────────────────────────────────────────────────────────────
# Table Enrichment via LLM
# ─────────────────────────────────────────────────────────────────────────────

async def enrich_table_with_llm(table: ParsedTableElement) -> ParsedTableElement:
    """
    Enrich a raw HTML table with LLM-generated Markdown and a natural language
    summary.

    Design decision — why enrich at ingestion time, not retrieval time?
      • Retrieval must be fast (sub-second). LLM enrichment adds 1-3 seconds.
      • Enrichment is idempotent for the same source table.
      • Pre-enriched chunks produce better embeddings since the semantic content
        of the table is expressed in natural language at index time.

    Error handling:
      • If the LLM call fails or returns malformed JSON, the original
        ParsedTableElement is returned unchanged with a log warning.
      • This makes enrichment a best-effort enhancement, not a blocking step.

    Args:
        table: Raw ParsedTableElement from unstructured.

    Returns:
        ParsedTableElement with enriched_markdown and llm_summary populated,
        or the original element if enrichment fails.
    """
    import json

    llm = get_llm_client()
    user_message = _TABLE_ENRICHMENT_USER_TEMPLATE.format(
        raw_html=table.raw_html[:8000],  # Truncate to avoid token overflow
        raw_text=table.raw_text[:2000],
    )

    try:
        raw_response = await llm.complete(
            system_prompt=_TABLE_ENRICHMENT_SYSTEM,
            user_message=user_message,
            temperature=0.0,   # Deterministic: same table → same Markdown always
            max_tokens=1024,
        )

        # Strip any accidental markdown code fences from the response
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip("`").strip()

        parsed = json.loads(cleaned)
        markdown_table: str = parsed.get("markdown_table", "").strip()
        summary: str = parsed.get("summary", "").strip()

        if not markdown_table:
            raise ValueError("LLM returned empty markdown_table.")

        logger.debug(
            "table_enrichment_complete",
            row_count=table.row_count,
            summary_preview=summary[:80],
        )

        # ParsedTableElement is not frozen — return updated version
        return ParsedTableElement(
            element_type="table",
            raw_html=table.raw_html,
            raw_text=table.raw_text,
            page_number=table.page_number,
            row_count=table.row_count,
            col_count=table.col_count,
            enriched_markdown=markdown_table,
            llm_summary=summary,
        )

    except Exception as exc:
        logger.warning(
            "table_enrichment_failed",
            error=str(exc),
            raw_text_preview=table.raw_text[:100],
        )
        return table  # Return original — enrichment is non-blocking


async def enrich_tables_concurrently(
    elements: list[ParsedElement],
    max_concurrent: int = 5,
) -> list[ParsedElement]:
    """
    Enrich all table elements concurrently, with a concurrency limiter.

    Uses asyncio.Semaphore to cap concurrent LLM calls, preventing rate-limit
    errors when a document contains many tables (e.g., a formulary PDF with
    50+ drug-tier tables).

    Args:
        elements: Mixed list of parsed elements (text, title, table).
        max_concurrent: Maximum concurrent LLM enrichment calls.

    Returns:
        Same list with TableElements replaced by enriched versions.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _enrich_with_limit(element: ParsedElement) -> ParsedElement:
        if not isinstance(element, ParsedTableElement):
            return element
        async with semaphore:
            return await enrich_table_with_llm(element)

    enriched = await asyncio.gather(*[_enrich_with_limit(e) for e in elements])
    table_count = sum(1 for e in elements if isinstance(e, ParsedTableElement))
    enriched_count = sum(
        1 for e in enriched
        if isinstance(e, ParsedTableElement) and e.enriched_markdown is not None
    )

    logger.info(
        "table_enrichment_batch_complete",
        total_tables=table_count,
        successfully_enriched=enriched_count,
    )
    return list(enriched)


# ─────────────────────────────────────────────────────────────────────────────
# Public API: Main Ingestion Entry Point
# ─────────────────────────────────────────────────────────────────────────────

async def ingest_pdf(
    file_path: Path,
    document_type: DocumentType = DocumentType.POLICY,
    enrich_tables: bool = True,
    strategy: str = "hi_res",
    chunking_strategy: str = "by_title",
) -> tuple[list[ParsedElement], dict[str, Any]]:
    """
    Full PDF ingestion pipeline: parse → convert → enrich.

    This is the entry point called by the ingestion orchestrator. It returns
    a list of typed ParsedElement DTOs ready to be passed to chunking.py.

    Pipeline steps:
      1. Run unstructured.partition_pdf in a thread pool (non-blocking)
      2. Convert raw unstructured elements to our typed DTO discriminated union
      3. Enrich all table elements concurrently via LLM (if enrich_tables=True)

    Args:
        file_path:        Absolute path to the PDF file.
        document_type:    DocumentType enum for metadata tagging.
        enrich_tables:    Whether to run LLM enrichment on extracted tables.
                          Set False for fast testing or when LLM is unavailable.
        strategy:         unstructured parsing strategy ('hi_res' | 'fast' | 'ocr_only').
                          'hi_res' requires detectron2 + tesseract.
                          Use 'fast' for plain-text PDFs without complex layouts.
        chunking_strategy: unstructured internal chunking strategy.

    Returns:
        Tuple of:
          - List[ParsedElement]: Typed, enriched elements ready for chunking.
          - dict: Ingestion statistics (element counts, timing, etc.)
    """
    if not file_path.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")
    if file_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {file_path.suffix}")

    stats: dict[str, Any] = {
        "filename": file_path.name,
        "document_type": document_type.value,
        "file_size_bytes": file_path.stat().st_size,
    }

    # ── Step 1: Parse PDF (blocking I/O + CPU in thread pool) ─────────────
    parse_start = time.perf_counter()
    raw_elements = await asyncio.to_thread(
        _parse_pdf_sync,
        file_path,
        strategy,
        chunking_strategy,
    )
    stats["parse_time_ms"] = (time.perf_counter() - parse_start) * 1000
    stats["raw_element_count"] = len(raw_elements)

    # ── Step 2: Convert to typed DTOs ─────────────────────────────────────
    typed_elements: list[ParsedElement] = []
    for raw in raw_elements:
        dto = _unstructured_element_to_dto(raw)
        if dto is not None:
            typed_elements.append(dto)

    stats["typed_element_count"] = len(typed_elements)
    stats["table_count"] = sum(1 for e in typed_elements if isinstance(e, ParsedTableElement))
    stats["title_count"] = sum(1 for e in typed_elements if isinstance(e, ParsedTitleElement))
    stats["text_count"] = sum(1 for e in typed_elements if isinstance(e, ParsedTextElement))

    logger.info(
        "pdf_elements_converted",
        filename=file_path.name,
        **{k: v for k, v in stats.items() if k.endswith("_count")},
    )

    # ── Step 3: LLM Table Enrichment ──────────────────────────────────────
    if enrich_tables and stats["table_count"] > 0:
        enrich_start = time.perf_counter()
        typed_elements = await enrich_tables_concurrently(typed_elements)
        stats["enrich_time_ms"] = (time.perf_counter() - enrich_start) * 1000
        stats["tables_enriched"] = sum(
            1 for e in typed_elements
            if isinstance(e, ParsedTableElement) and e.enriched_markdown is not None
        )

    stats["total_time_ms"] = (time.perf_counter() - parse_start) * 1000

    logger.info(
        "pdf_ingestion_complete",
        filename=file_path.name,
        total_elements=len(typed_elements),
        total_ms=f"{stats['total_time_ms']:.1f}",
    )

    return typed_elements, stats


async def ingest_raw_text(
    text: str,
    document_type: DocumentType = DocumentType.UNKNOWN,
    source_filename: str = "inline_text.txt",
) -> tuple[list[ParsedElement], dict[str, Any]]:
    """
    Ingest pre-extracted plain text (e.g., from EDI files, email bodies,
    or text already extracted from non-PDF sources).

    Applies simple paragraph-boundary splitting as a pre-chunking step.
    No unstructured dependency required.

    Args:
        text:            Raw text content.
        document_type:   DocumentType for metadata tagging.
        source_filename: Logical filename for audit purposes.

    Returns:
        Tuple of (List[ParsedElement], stats_dict).
    """
    start = time.perf_counter()

    # Split on double newlines (paragraph boundaries)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    elements: list[ParsedElement] = [
        ParsedTextElement(text=para, page_number=None)
        for para in paragraphs
    ]

    stats = {
        "filename": source_filename,
        "document_type": document_type.value,
        "raw_element_count": len(elements),
        "parse_time_ms": (time.perf_counter() - start) * 1000,
    }

    return elements, stats
