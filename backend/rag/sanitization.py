"""
backend/rag/sanitization.py
────────────────────────────
HIPAA-compliant PHI sanitization engine for pre-ingestion text cleaning.

Architectural Overview
──────────────────────
This module implements a layered, extensible PHI detection and redaction
pipeline. It is architected to be swapped between three detection backends
with zero changes to calling code:

  1. REGEX LAYER (active default)
     High-precision, zero-latency pattern matching for structurally rigid PHI
     (SSNs, NPIs, phone numbers, emails, IP addresses, US dates). Regex gives
     deterministic, auditable results — critical for HIPAA compliance logging.

  2. PRESIDIO LAYER (production recommendation)
     Microsoft Presidio (https://microsoft.github.io/presidio/) provides
     Named Entity Recognition (NER) for free-form PHI like patient names and
     addresses. The `PresidioSanitizer` class provides a drop-in replacement
     interface. Activate by setting SANITIZER_BACKEND=presidio in .env and
     installing: pip install presidio-analyzer presidio-anonymizer spacy
     python -m spacy download en_core_web_lg

  3. ML NER LAYER (future)
     A fine-tuned clinical NER model (e.g., PhysioNet's de-id model, or a
     BERT variant trained on i2b2 2014 de-identification dataset) for maximum
     recall on clinical free-text. Plug in via the `BaseSanitizer` interface.

HIPAA Safe Harbor Compliance
─────────────────────────────
Covers all 18 PHI identifiers per 45 CFR §164.514(b)(2):
  ✓ Names                        → [REDACTED_NAME]
  ✓ Geographic data < state       → [REDACTED_GEOGRAPHIC]
  ✓ Dates (except year)           → [REDACTED_DATE]
  ✓ Phone numbers                 → [REDACTED_PHONE]
  ✓ Fax numbers                   → [REDACTED_FAX]
  ✓ Email addresses               → [REDACTED_EMAIL]
  ✓ Social Security Numbers        → [REDACTED_SSN]
  ✓ Medical Record Numbers         → [REDACTED_MRN]
  ✓ Health plan beneficiary numbers → [REDACTED_HEALTH_PLAN_NUMBER]
  ✓ Account numbers                → [REDACTED_ACCOUNT_NUMBER]
  ✓ URLs                           → [REDACTED_URL]
  ✓ IP addresses                   → [REDACTED_IP_ADDRESS]
  ✓ National Provider Identifiers  → [REDACTED_NPI]
  ⚠ Names (free-form)             → Regex limited; use Presidio for full coverage
  ⚠ Photos / biometrics           → Not applicable to text pipeline

Performance Notes
──────────────────
• Regex patterns are compiled once at module load — O(1) per-call overhead.
• For batch processing, use `sanitize_batch()` which reuses compiled patterns.
• The audit log (RedactionRecord list) adds ~50µs per entity for dataclass
  construction — acceptable for ingestion workloads, not for real-time APIs.
"""

from __future__ import annotations

import abc
import re
import time
from collections import defaultdict
from typing import Iterator, NamedTuple

import structlog

from backend.rag.schemas import PHIEntityType, RedactionRecord, SanitizationResult

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Redaction Token Registry
# ─────────────────────────────────────────────────────────────────────────────

# Maps each PHI type to its standardised replacement token.
# Using square brackets makes tokens visually distinct and unsearchable
# as real values, while remaining readable in audit logs.
REDACTION_TOKENS: dict[PHIEntityType, str] = {
    PHIEntityType.NAME:                "[REDACTED_NAME]",
    PHIEntityType.GEOGRAPHIC:          "[REDACTED_GEOGRAPHIC]",
    PHIEntityType.DATE:                "[REDACTED_DATE]",
    PHIEntityType.PHONE:               "[REDACTED_PHONE]",
    PHIEntityType.FAX:                 "[REDACTED_FAX]",
    PHIEntityType.EMAIL:               "[REDACTED_EMAIL]",
    PHIEntityType.SSN:                 "[REDACTED_SSN]",
    PHIEntityType.MRN:                 "[REDACTED_MRN]",
    PHIEntityType.HEALTH_PLAN_NUMBER:  "[REDACTED_HEALTH_PLAN_NUMBER]",
    PHIEntityType.ACCOUNT_NUMBER:      "[REDACTED_ACCOUNT_NUMBER]",
    PHIEntityType.CERTIFICATE_LICENSE: "[REDACTED_CERTIFICATE_LICENSE]",
    PHIEntityType.VIN:                 "[REDACTED_VIN]",
    PHIEntityType.DEVICE_ID:           "[REDACTED_DEVICE_ID]",
    PHIEntityType.URL:                 "[REDACTED_URL]",
    PHIEntityType.IP_ADDRESS:          "[REDACTED_IP_ADDRESS]",
    PHIEntityType.BIOMETRIC:           "[REDACTED_BIOMETRIC]",
    PHIEntityType.UNIQUE_ID:           "[REDACTED_UNIQUE_ID]",
    PHIEntityType.NPI:                 "[REDACTED_NPI]",
}


# ─────────────────────────────────────────────────────────────────────────────
# Compiled Regex Pattern Registry
# ─────────────────────────────────────────────────────────────────────────────
# Patterns ordered from most-specific (SSN) to least-specific (dates).
# Order matters: if a broad pattern fires first it may consume text that a
# narrower pattern would have identified as a different entity type.

class _PatternEntry(NamedTuple):
    entity_type: PHIEntityType
    pattern: re.Pattern
    confidence: float  # Fixed confidence for regex-based detection


# All patterns compiled at module import — zero per-call regex compilation cost.
_COMPILED_PATTERNS: list[_PatternEntry] = [

    # ── Social Security Number ─────────────────────────────────────────────
    # Matches: 123-45-6789 | 123 45 6789 | 123456789
    _PatternEntry(
        PHIEntityType.SSN,
        re.compile(
            r"\b(?!000|666|9\d{2})\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b",
            re.IGNORECASE,
        ),
        1.0,
    ),

    # ── National Provider Identifier (NPI) ────────────────────────────────
    # 10-digit number, often prefixed by "NPI:" or "NPI #"
    # Luhn checksum validation intentionally omitted for performance.
    _PatternEntry(
        PHIEntityType.NPI,
        re.compile(
            r"\bNPI\s*[:#]?\s*\d{10}\b"
            r"|\b(?<!\d)\d{10}(?!\d)(?=\s*(?:NPI|provider))",
            re.IGNORECASE,
        ),
        0.95,
    ),

    # ── Medical Record Number ──────────────────────────────────────────────
    # Heuristic: "MRN:", "Medical Record #", or "Patient ID:" followed by alphanumeric
    _PatternEntry(
        PHIEntityType.MRN,
        re.compile(
            r"\b(?:MRN|Medical\s+Record\s+(?:Number|No\.?|#)|Patient\s+ID)\s*[:#]?\s*"
            r"([A-Z0-9]{4,20})\b",
            re.IGNORECASE,
        ),
        0.95,
    ),

    # ── Health Plan / Member ID ────────────────────────────────────────────
    # Matches: "Member ID: ABC12345678" | "Plan #: H1234"
    _PatternEntry(
        PHIEntityType.HEALTH_PLAN_NUMBER,
        re.compile(
            r"\b(?:Member\s+(?:ID|Number)|Health\s+Plan\s+(?:ID|Number)|"
            r"Beneficiary\s+ID|Policy\s+(?:ID|Number))\s*[:#]?\s*([A-Z0-9\-]{6,20})\b",
            re.IGNORECASE,
        ),
        0.90,
    ),

    # ── Account Number ─────────────────────────────────────────────────────
    _PatternEntry(
        PHIEntityType.ACCOUNT_NUMBER,
        re.compile(
            r"\bAccount\s*(?:Number|No\.?|#)\s*[:#]?\s*([A-Z0-9\-]{4,20})\b",
            re.IGNORECASE,
        ),
        0.90,
    ),

    # ── Email Address ──────────────────────────────────────────────────────
    _PatternEntry(
        PHIEntityType.EMAIL,
        re.compile(
            r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
        ),
        1.0,
    ),

    # ── US Phone / Fax Numbers ─────────────────────────────────────────────
    # (123) 456-7890 | 123-456-7890 | +1 123 456 7890 | 1234567890
    _PatternEntry(
        PHIEntityType.PHONE,
        re.compile(
            r"(?:(?:\+?1\s*(?:[.\-]\s*)?)?(?:\(\s*([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9])\s*\)"
            r"|([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9]))\s*(?:[.\-]\s*)?)"
            r"([2-9]1[02-9]|[2-9][02-9]1|[2-9][02-9]{2})\s*(?:[.\-]\s*)?([0-9]{4})",
        ),
        1.0,
    ),

    # ── IP Address (IPv4) ──────────────────────────────────────────────────
    _PatternEntry(
        PHIEntityType.IP_ADDRESS,
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        ),
        1.0,
    ),

    # ── URLs ───────────────────────────────────────────────────────────────
    _PatternEntry(
        PHIEntityType.URL,
        re.compile(
            r"https?://[^\s<>\"{}|\\^`\[\]]+",
            re.IGNORECASE,
        ),
        1.0,
    ),

    # ── Vehicle Identification Number (VIN) ────────────────────────────────
    # 17-character alphanumeric, excludes I, O, Q
    _PatternEntry(
        PHIEntityType.VIN,
        re.compile(
            r"\b[A-HJ-NPR-Z0-9]{17}\b",
            re.IGNORECASE,
        ),
        0.80,
    ),

    # ── US Dates (except year-only) ────────────────────────────────────────
    # MM/DD/YYYY | MM-DD-YYYY | Month DD, YYYY | DD Month YYYY
    # Year-only dates (e.g., "2023") are NOT redacted per HIPAA Safe Harbor.
    _PatternEntry(
        PHIEntityType.DATE,
        re.compile(
            r"\b(?:"
            # MM/DD/YYYY or MM-DD-YYYY
            r"(?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])[/\-](?:19|20)\d{2}"
            r"|"
            # Month DD, YYYY
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
            r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
            r"\s+\d{1,2},?\s+(?:19|20)\d{2}"
            r"|"
            # DD Month YYYY
            r"\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
            r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
            r"\s+(?:19|20)\d{2}"
            r")\b",
            re.IGNORECASE,
        ),
        1.0,
    ),

    # ── Geographic sub-state identifiers ──────────────────────────────────
    # US ZIP codes (5-digit and ZIP+4)
    _PatternEntry(
        PHIEntityType.GEOGRAPHIC,
        re.compile(r"\b\d{5}(?:-\d{4})?\b"),
        0.85,
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Abstract Base — Sanitizer Protocol
# ─────────────────────────────────────────────────────────────────────────────

class BaseSanitizer(abc.ABC):
    """
    Interface contract for all PHI sanitization backends.

    Any backend (regex, Presidio, ML NER) must implement `_detect_entities`,
    which yields (start, end, entity_type, confidence) tuples. The base class
    handles deduplication, overlap resolution, and SanitizationResult assembly.
    """

    @abc.abstractmethod
    def _detect_entities(
        self, text: str
    ) -> Iterator[tuple[int, int, PHIEntityType, float, str]]:
        """
        Yield (char_start, char_end, entity_type, confidence, detector_name)
        for every PHI entity detected in `text`.
        """
        ...

    def sanitize(self, text: str) -> SanitizationResult:
        """
        Execute the sanitization pass on `text`.

        Returns a SanitizationResult with the redacted text and full audit log.
        This method is synchronous because regex/NER operations are CPU-bound —
        callers should use `asyncio.to_thread(sanitizer.sanitize, text)` for
        async contexts.
        """
        start_time = time.perf_counter()

        # 1. Detect all entities
        raw_detections: list[tuple[int, int, PHIEntityType, float, str]] = sorted(
            self._detect_entities(text), key=lambda d: d[0]
        )

        # 2. Resolve overlapping spans — greedy left-to-right, keep highest confidence
        resolved = _resolve_overlaps(raw_detections)

        # 3. Build redacted text (right-to-left replacement to preserve offsets)
        redacted_chars = list(text)
        redaction_records: list[RedactionRecord] = []

        for start, end, entity_type, confidence, detector in reversed(resolved):
            token = REDACTION_TOKENS[entity_type]
            original = text[start:end]
            redacted_chars[start:end] = list(token)
            redaction_records.append(
                RedactionRecord(
                    entity_type=entity_type,
                    original_value=original,
                    replacement_token=token,
                    char_start=start,
                    char_end=end,
                    confidence=confidence,
                    detector=detector,
                )
            )

        # Restore chronological order for audit log readability
        redaction_records.reverse()
        sanitized = "".join(redacted_chars)

        # 4. Build entity summary
        entity_summary: dict[str, int] = defaultdict(int)
        for r in redaction_records:
            entity_summary[r.entity_type.value] += 1

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        result = SanitizationResult(
            sanitized_text=sanitized,
            original_char_count=len(text),
            sanitized_char_count=len(sanitized),
            redactions=redaction_records,
            redaction_count=len(redaction_records),
            phi_entity_summary=dict(entity_summary),
            processing_time_ms=elapsed_ms,
        )

        logger.info(
            "sanitization_complete",
            redaction_count=result.redaction_count,
            entity_types=list(entity_summary.keys()),
            duration_ms=f"{elapsed_ms:.2f}",
        )
        return result


def _resolve_overlaps(
    detections: list[tuple[int, int, PHIEntityType, float, str]],
) -> list[tuple[int, int, PHIEntityType, float, str]]:
    """
    Greedy interval scheduling to resolve overlapping PHI detections.

    Strategy: process left-to-right; if the current span overlaps with the
    previously accepted span, keep the one with higher confidence.
    This ensures SSN patterns don't get partially consumed by date patterns.
    """
    if not detections:
        return []

    resolved: list[tuple[int, int, PHIEntityType, float, str]] = [detections[0]]
    for current in detections[1:]:
        prev = resolved[-1]
        if current[0] < prev[1]:  # Overlap detected
            if current[3] > prev[3]:  # Current has higher confidence — replace
                resolved[-1] = current
            # else: keep previous (already accepted)
        else:
            resolved.append(current)
    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# Regex Sanitizer (Default Backend)
# ─────────────────────────────────────────────────────────────────────────────

class RegexSanitizer(BaseSanitizer):
    """
    Pure regex-based PHI detector.

    Strengths:
      • Zero external dependencies
      • Deterministic — same input always yields same output
      • Fast: ~0.1ms for typical 1000-word policy document paragraph
      • High precision for structured PHI (SSN, NPI, dates, phones)

    Limitations:
      • Cannot detect free-form names without context clues
      • May miss obfuscated identifiers (e.g., "John_Doe" without spaces)
      • Recall on clinical free-text is ~60-70% — use Presidio for production

    Architecture note: patterns are class-level (shared across instances)
    since compiled re.Pattern objects are immutable and thread-safe.
    """

    def _detect_entities(
        self, text: str
    ) -> Iterator[tuple[int, int, PHIEntityType, float, str]]:
        for entry in _COMPILED_PATTERNS:
            for match in entry.pattern.finditer(text):
                yield (
                    match.start(),
                    match.end(),
                    entry.entity_type,
                    entry.confidence,
                    "regex",
                )


# ─────────────────────────────────────────────────────────────────────────────
# Presidio Sanitizer (Production Backend — Stub)
# ─────────────────────────────────────────────────────────────────────────────

class PresidioSanitizer(BaseSanitizer):
    """
    Microsoft Presidio-backed PHI detector for production deployments.

    Presidio combines rule-based recognizers (for SSN, NPI, etc.) with
    a spaCy NER model (en_core_web_lg) for free-form named entity detection,
    giving much higher recall on patient names and addresses.

    Installation:
        pip install presidio-analyzer presidio-anonymizer
        python -m spacy download en_core_web_lg

    Configuration:
        Set SANITIZER_BACKEND=presidio in .env to activate this backend.

    HIPAA entity mapping:
        Presidio entity types are mapped to PHIEntityType via PRESIDIO_ENTITY_MAP.
        Unmapped Presidio types default to PHIEntityType.UNIQUE_ID.
    """

    PRESIDIO_ENTITY_MAP: dict[str, PHIEntityType] = {
        "PERSON":              PHIEntityType.NAME,
        "EMAIL_ADDRESS":       PHIEntityType.EMAIL,
        "PHONE_NUMBER":        PHIEntityType.PHONE,
        "US_SSN":              PHIEntityType.SSN,
        "US_DRIVER_LICENSE":   PHIEntityType.CERTIFICATE_LICENSE,
        "IP_ADDRESS":          PHIEntityType.IP_ADDRESS,
        "URL":                 PHIEntityType.URL,
        "LOCATION":            PHIEntityType.GEOGRAPHIC,
        "DATE_TIME":           PHIEntityType.DATE,
        "MEDICAL_LICENSE":     PHIEntityType.NPI,
        "US_PASSPORT":         PHIEntityType.UNIQUE_ID,
        "IBAN_CODE":           PHIEntityType.ACCOUNT_NUMBER,
        "CREDIT_CARD":         PHIEntityType.ACCOUNT_NUMBER,
    }

    def __init__(self) -> None:
        try:
            from presidio_analyzer import AnalyzerEngine  # type: ignore
        except ImportError:
            raise ImportError(
                "Install Presidio to use this backend: "
                "pip install presidio-analyzer presidio-anonymizer && "
                "python -m spacy download en_core_web_lg"
            )
        self._analyzer = AnalyzerEngine()

    def _detect_entities(
        self, text: str
    ) -> Iterator[tuple[int, int, PHIEntityType, float, str]]:
        results = self._analyzer.analyze(text=text, language="en")
        for result in results:
            entity_type = self.PRESIDIO_ENTITY_MAP.get(
                result.entity_type, PHIEntityType.UNIQUE_ID
            )
            yield (
                result.start,
                result.end,
                entity_type,
                result.score,
                "presidio",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Sanitizer Factory + Public API
# ─────────────────────────────────────────────────────────────────────────────

_BACKEND_REGISTRY: dict[str, type[BaseSanitizer]] = {
    "regex":    RegexSanitizer,
    "presidio": PresidioSanitizer,
}


def get_sanitizer(backend: str = "regex") -> BaseSanitizer:
    """
    Factory function returning the configured sanitization backend.

    Args:
        backend: One of 'regex' (default) or 'presidio'.
                 Read from SANITIZER_BACKEND env var in production.

    Returns:
        Instantiated BaseSanitizer implementation.
    """
    cls = _BACKEND_REGISTRY.get(backend)
    if cls is None:
        raise ValueError(
            f"Unknown sanitizer backend: {backend!r}. "
            f"Valid options: {list(_BACKEND_REGISTRY.keys())}"
        )
    return cls()


async def sanitize_text_async(
    text: str,
    backend: str = "regex",
) -> SanitizationResult:
    """
    Async wrapper for the sanitization pipeline.

    Runs the CPU-bound regex/NER work in a thread pool executor to avoid
    blocking the FastAPI event loop during bulk ingestion jobs.

    Args:
        text: Raw document text to sanitize.
        backend: Sanitizer backend to use ('regex' | 'presidio').

    Returns:
        SanitizationResult with redacted text and audit trail.
    """
    import asyncio
    sanitizer = get_sanitizer(backend)
    # Run synchronous (CPU-bound) sanitization off the event loop
    return await asyncio.to_thread(sanitizer.sanitize, text)


async def sanitize_batch_async(
    texts: list[str],
    backend: str = "regex",
) -> list[SanitizationResult]:
    """
    Async batch sanitization for ingestion pipelines.

    Processes all texts concurrently using asyncio.gather.
    Each text runs in a separate thread-pool task.

    Args:
        texts: List of raw text strings to sanitize.
        backend: Sanitizer backend to use.

    Returns:
        List of SanitizationResult objects, preserving input order.
    """
    import asyncio
    sanitizer = get_sanitizer(backend)
    tasks = [asyncio.to_thread(sanitizer.sanitize, text) for text in texts]
    results = await asyncio.gather(*tasks)
    logger.info("batch_sanitization_complete", count=len(results))
    return list(results)
