"""
backend/rag/schemas.py
───────────────────────
Pydantic Data Transfer Objects (DTOs) for the document ingestion pipeline.

Architectural decisions:
─────────────────────────
1. STRICT TYPING OVER FLEXIBILITY
   Every chunk that exits the ingestion pipeline is validated through these
   models. This prevents malformed metadata from silently polluting the vector
   database, which is especially critical in a multi-tenant healthcare platform
   where a metadata error could expose one tenant's documents to another.

2. METADATA AS A FIRST-CLASS CITIZEN
   The `ChunkMetadata` model is intentionally rich. ChromaDB stores metadata
   as a flat key-value dict alongside each vector. By encoding RBAC roles,
   document type, and tenant identity here, we enable:
     • Pre-retrieval filtering: ChromaDB `where` clauses filter by tenant_id
       and allowed_roles BEFORE semantic search — this is a critical security
       boundary in multi-tenant RAG.
     • Post-retrieval auditing: every chunk carries its provenance.

3. PHI AUDIT TRAIL
   `RedactionRecord` and `SanitizationResult` create a structured audit log of
   every PHI entity that was detected and redacted. In a HIPAA-covered system
   this log must itself be stored securely (not in the vector DB) for breach
   investigation.

4. IMMUTABLE CHUNKS
   `DocumentChunk` uses `model_config = {"frozen": True}` to enforce immutability
   after construction. A chunk's identity is its content + metadata — mutation
   after construction would silently create a hash mismatch in the vector store.

5. DISCRIMINATED UNION FOR PARSED ELEMENTS
   `ParsedElement` is a discriminated union covering text blocks, tables, and
   titles. This allows the ingestion pipeline to handle each element type
   with its own enrichment strategy without isinstance() proliferation.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class DocumentType(str, enum.Enum):
    """
    Classifies the source document type.
    Used to select the appropriate chunking strategy and prompt template.
    """
    POLICY = "policy"                    # Insurance policy contract PDF
    EOB = "eob"                          # Explanation of Benefits
    CLAIM_FORM = "claim_form"            # CMS-1500 / UB-04 claim forms
    CLINICAL_NOTE = "clinical_note"      # Physician notes, discharge summaries
    FORMULARY = "formulary"              # Drug formulary PDFs
    UNDERWRITING_GUIDELINE = "underwriting_guideline"
    REGULATORY = "regulatory"            # CMS, ACA, state regulation docs
    PRIOR_AUTH = "prior_auth"            # Prior authorization forms
    REMITTANCE = "remittance"            # ERA / 835 remittance advice
    UNKNOWN = "unknown"


class PHIEntityType(str, enum.Enum):
    """
    HIPAA Safe Harbor identifiers (45 CFR §164.514(b)(2)).
    Maps directly to the 18 PHI identifiers the HIPAA Privacy Rule mandates
    must be de-identified before data can be used or disclosed.
    """
    NAME = "NAME"
    GEOGRAPHIC = "GEOGRAPHIC"           # All geographic data smaller than state
    DATE = "DATE"                       # Dates except year (DOB, admission, discharge)
    PHONE = "PHONE"
    FAX = "FAX"
    EMAIL = "EMAIL"
    SSN = "SSN"                         # Social Security Number
    MRN = "MRN"                         # Medical Record Number
    HEALTH_PLAN_NUMBER = "HEALTH_PLAN_NUMBER"
    ACCOUNT_NUMBER = "ACCOUNT_NUMBER"
    CERTIFICATE_LICENSE = "CERTIFICATE_LICENSE"
    VIN = "VIN"                         # Vehicle identifiers
    DEVICE_ID = "DEVICE_ID"
    URL = "URL"
    IP_ADDRESS = "IP_ADDRESS"
    BIOMETRIC = "BIOMETRIC"
    PHOTO = "PHOTO"
    UNIQUE_ID = "UNIQUE_ID"             # Any other unique identifier
    NPI = "NPI"                         # National Provider Identifier (10-digit)


class ChunkingStrategy(str, enum.Enum):
    """Strategy used to produce this chunk — tracked for reproducibility."""
    SEMANTIC = "semantic"               # Sentence-embedding coherence grouping
    BY_TITLE = "by_title"              # Unstructured 'by_title' structural chunking
    TABLE = "table"                     # Extracted table element
    FIXED_SIZE = "fixed_size"           # Naive fallback (deprecated — avoid)


# ─────────────────────────────────────────────────────────────────────────────
# PHI Redaction Audit Models
# ─────────────────────────────────────────────────────────────────────────────

class RedactionRecord(BaseModel):
    """
    Immutable record of a single PHI entity detected and redacted.
    Stored in an audit log — never in the vector database.
    """
    model_config = {"frozen": True}

    entity_type: PHIEntityType
    original_value: str = Field(
        description="The original PHI value before redaction. "
                    "NEVER store this in the vector DB.",
    )
    replacement_token: str = Field(
        description="The standardised token that replaced the PHI, e.g. [REDACTED_SSN].",
    )
    char_start: int = Field(description="Character offset in the original text.")
    char_end: int = Field(description="Character offset end in the original text.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Detection confidence score (1.0 for regex exact match).",
    )
    detector: str = Field(
        default="regex",
        description="Detector that found this entity: 'regex', 'presidio', 'ml_ner'.",
    )


class SanitizationResult(BaseModel):
    """
    Output of the HIPAA sanitization step.
    Contains the clean text and a complete audit trail of all redactions.
    """
    model_config = {"frozen": True}

    sanitized_text: str = Field(
        description="The text with all detected PHI replaced by standardised tokens.",
    )
    original_char_count: int
    sanitized_char_count: int
    redactions: list[RedactionRecord] = Field(
        default_factory=list,
        description="Ordered list of all PHI entities that were redacted.",
    )
    redaction_count: int = Field(
        description="Total number of PHI entities redacted. "
                    "Derived from len(redactions) — validated by model_validator.",
    )
    phi_entity_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Count of each PHIEntityType found, e.g. {'SSN': 2, 'NAME': 7}.",
    )
    processing_time_ms: float = Field(
        description="Wall-clock time for the sanitization pass in milliseconds.",
    )

    @model_validator(mode="after")
    def _validate_counts(self) -> "SanitizationResult":
        assert self.redaction_count == len(self.redactions), (
            f"redaction_count ({self.redaction_count}) must equal "
            f"len(redactions) ({len(self.redactions)})"
        )
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Parsed Element DTOs (from unstructured / PDF parsing)
# ─────────────────────────────────────────────────────────────────────────────

class ParsedTextElement(BaseModel):
    """A plain text block extracted from a document."""
    element_type: Literal["text"] = "text"
    text: str
    page_number: int | None = None
    coordinates: dict[str, float] | None = Field(
        None,
        description="Bounding box on the page: {x1, y1, x2, y2} in points.",
    )


class ParsedTitleElement(BaseModel):
    """
    A heading or section title.
    Critical for by_title chunking: titles act as semantic boundaries
    that prevent adjacent sections from being merged into one chunk.
    """
    element_type: Literal["title"] = "title"
    text: str
    heading_level: int = Field(
        default=1, ge=1, le=6,
        description="Inferred heading depth (1=H1, 6=H6).",
    )
    page_number: int | None = None


class ParsedTableElement(BaseModel):
    """
    A table extracted from a document.

    Raw HTML representation is preserved from unstructured for maximum
    fidelity. The `enriched_markdown` and `llm_summary` fields are
    populated by the LLM enrichment step in ingestion.py.
    """
    element_type: Literal["table"] = "table"
    raw_html: str = Field(description="Raw HTML table as extracted by unstructured.")
    raw_text: str = Field(
        description="Plain-text fallback extracted from the table cells.",
    )
    page_number: int | None = None
    row_count: int | None = None
    col_count: int | None = None
    # Populated by LLM enrichment step
    enriched_markdown: str | None = Field(
        None,
        description="LLM-converted Markdown table for embedding. Cleaner than raw HTML.",
    )
    llm_summary: str | None = Field(
        None,
        description="1-2 sentence natural language summary generated by LLM "
                    "to give the table semantic context during retrieval.",
    )


# Discriminated union — allows pipeline to type-switch cleanly
ParsedElement = ParsedTextElement | ParsedTitleElement | ParsedTableElement


# ─────────────────────────────────────────────────────────────────────────────
# Core DTO: DocumentChunk
# ─────────────────────────────────────────────────────────────────────────────

class ChunkMetadata(BaseModel):
    """
    Highly structured metadata attached to every DocumentChunk.

    Design note — why so much metadata?
    ChromaDB allows filtering by metadata fields during retrieval (the `where`
    parameter). By embedding RBAC, tenant identity, and document provenance
    here, we can enforce data isolation and role restrictions AT THE VECTOR DB
    LAYER, before any chunk text is returned to the application:

        collection.query(
            query_embeddings=[...],
            where={
                "$and": [
                    {"tenant_id": {"$eq": "acme-insurance"}},
                    {"allowed_roles": {"$contains": "claims_adjuster"}},
                ]
            }
        )
    """
    model_config = {"frozen": True}

    # ── Multi-tenancy ──────────────────────────────────────────────────────
    tenant_id: str = Field(
        description="Unique identifier for the tenant (insurance org). "
                    "Primary isolation boundary in multi-tenant deployments.",
    )
    document_id: str = Field(
        description="UUID of the source document in PostgreSQL documents table.",
    )

    # ── Document Provenance ────────────────────────────────────────────────
    source_filename: str = Field(
        description="Original filename (basename only — never full path for security).",
    )
    document_type: DocumentType
    page_number: int | None = Field(
        None,
        description="Page number in the source PDF (1-indexed). "
                    "None for non-paginated sources.",
    )
    section_title: str | None = Field(
        None,
        description="Nearest parent section title from the document structure. "
                    "Populated by by_title chunking; used to improve retrieval context.",
    )

    # ── Chunk Identity ─────────────────────────────────────────────────────
    chunk_index: int = Field(
        description="Zero-indexed position of this chunk within its source document.",
        ge=0,
    )
    chunk_id: str = Field(
        description="Globally unique chunk identifier: '{document_id}_chunk_{chunk_index}'.",
    )
    chunking_strategy: ChunkingStrategy

    # ── Content Flags ──────────────────────────────────────────────────────
    is_table: bool = Field(
        description="True if this chunk represents an extracted table. "
                    "Table chunks are embedded with their LLM-generated summary "
                    "prepended for richer semantic context.",
    )
    is_sanitized: bool = Field(
        description="True if HIPAA sanitization has been applied to this chunk's text.",
        default=True,
    )
    phi_redaction_count: int = Field(
        default=0,
        description="Number of PHI entities redacted from this chunk. "
                    "Non-zero values indicate original document contained PHI.",
    )

    # ── RBAC ──────────────────────────────────────────────────────────────
    allowed_roles: list[str] = Field(
        description="List of UserRole values permitted to retrieve this chunk. "
                    "Example: ['admin', 'claims_adjuster']. "
                    "Used in ChromaDB where-filter for pre-retrieval RBAC.",
        min_length=1,
    )

    # ── Temporal ──────────────────────────────────────────────────────────
    ingested_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp when this chunk was ingested into the vector store.",
    )
    document_effective_date: date | None = Field(
        None,
        description="Effective date of the source policy/document, if parseable. "
                    "Enables time-aware retrieval (e.g., 'policy as of claim date').",
    )

    # ── Token & Size Metrics ───────────────────────────────────────────────
    char_count: int = Field(ge=0)
    token_estimate: int = Field(
        ge=0,
        description="Rough token count estimate (char_count // 4). "
                    "Actual tokenization varies by model.",
    )

    @field_validator("chunk_id")
    @classmethod
    def _validate_chunk_id_format(cls, v: str) -> str:
        if "_chunk_" not in v:
            raise ValueError("chunk_id must follow format: '{document_id}_chunk_{index}'")
        return v

    @field_validator("allowed_roles")
    @classmethod
    def _validate_roles_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("allowed_roles must contain at least one role.")
        return v


class DocumentChunk(BaseModel):
    """
    The canonical output unit of the ingestion + chunking pipeline.

    Every DocumentChunk that enters the vector database must be an instance
    of this model — the vectorstore service accepts only DocumentChunk objects,
    never raw strings. This enforces the pipeline contract at the type level.

    Embedding vector lifecycle:
      • `embedding` is None immediately after chunking.
      • It is populated by `embeddings/service.py` before upsert to ChromaDB.
      • The vector is NOT persisted in PostgreSQL — ChromaDB owns it.

    Text content contract:
      • `text` MUST be the post-sanitization content (PHI already redacted).
      • For table chunks, `text` is: "{llm_summary}\n\n{enriched_markdown}",
        which gives the embedding both semantic context and structured data.
      • `raw_text_preview` is an optional debugging field (truncated to 200 chars).
    """
    model_config = {"frozen": True}

    chunk_id: str = Field(description="Mirrors ChunkMetadata.chunk_id — the vector DB document ID.")
    text: str = Field(
        description="Sanitized, embedding-ready text content of this chunk. "
                    "For tables: LLM summary + Markdown. For text/title: prose.",
        min_length=1,
    )
    metadata: ChunkMetadata
    embedding: list[float] | None = Field(
        None,
        description="Dense embedding vector. Populated by embeddings/service.py "
                    "before insertion into ChromaDB. None until embedded.",
    )
    raw_text_preview: str | None = Field(
        None,
        max_length=200,
        description="First 200 chars of text for debugging. Never stored in vector DB.",
    )

    @model_validator(mode="after")
    def _sync_chunk_id(self) -> "DocumentChunk":
        if self.chunk_id != self.metadata.chunk_id:
            raise ValueError(
                f"DocumentChunk.chunk_id ({self.chunk_id!r}) must match "
                f"metadata.chunk_id ({self.metadata.chunk_id!r})"
            )
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline Result DTOs
# ─────────────────────────────────────────────────────────────────────────────

class IngestionResult(BaseModel):
    """
    Summary result returned after a full document ingestion run.
    Provides observability metrics for the ingestion orchestrator.
    """
    document_id: str
    source_filename: str
    document_type: DocumentType
    tenant_id: str

    # Counts
    total_parsed_elements: int = Field(description="Raw elements from unstructured.")
    table_elements_found: int
    table_elements_enriched: int = Field(description="Tables successfully LLM-enriched.")
    total_chunks_produced: int
    total_chunks_upserted: int = Field(description="Chunks successfully written to ChromaDB.")

    # PHI summary
    total_phi_redactions: int
    phi_entity_breakdown: dict[str, int] = Field(
        default_factory=dict,
        description="Aggregate PHI entity counts across all chunks.",
    )

    # Performance
    parse_time_ms: float
    sanitization_time_ms: float
    chunking_time_ms: float
    embedding_time_ms: float
    total_time_ms: float

    # Status
    succeeded: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    ingested_at: datetime = Field(default_factory=datetime.utcnow)
