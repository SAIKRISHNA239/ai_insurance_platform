"""
backend/claims/snip_validator.py
──────────────────────────────────
7-Tier SNIP (Sequentially Numbered Information Process) Validation Engine.

SNIP defines 7 progressive validation tiers for EDI 837 claim payloads.
Each tier is a strict gate — failure at any tier immediately rejects the
claim and no further tiers run. This mirrors real clearinghouse behavior
where a 999 rejection is issued at the first failing tier.

FAILURE SAFETY — CLAIMS NEVER DROPPED
───────────────────────────────────────
Every SNIPValidationError carries the claim_id, failing tier, and a
machine-readable error_code. The router catches this exception and:
  1. Persists the claim to PostgreSQL with status=SNIP_REJECTED.
  2. Writes the full SNIPResult to the claim's ai_metadata JSONB column.
  3. Returns HTTP 422 with the structured error to the submitter.

A claim is NEVER silently dropped. Even rejected claims are persisted for
CMS audit trail requirements (45 CFR §162.1601 transaction standards).

BALANCE TESTING — TIER 3
─────────────────────────
The sum of all line-item charges must equal the header total_charge field.
We use Python `Decimal` arithmetic exclusively — floating-point rounding
in financial calculations is a CMS compliance violation. The tolerance is
exactly $0.00 (zero) for institutional claims; some clearinghouses allow
±$0.01 for professional claims with rounding, but we enforce strict equality
to maintain the highest standard of integrity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import IntEnum
from typing import Any

import structlog

from backend.claims.schemas import EDIClaimPayload

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SNIP Tier Enumeration
# ─────────────────────────────────────────────────────────────────────────────

class SNIPTier(IntEnum):
    INTEGRITY        = 1  # Schema / data type validation
    HIPAA_COMPLIANCE = 2  # Mandatory NPI and demographic fields
    BALANCE_TESTING  = 3  # Line-item charges == header total
    INTER_SEGMENT    = 4  # Cross-segment value consistency (stub)
    EXTERNAL_CODE    = 5  # ICD-10 / CPT code set validity (stub)
    BALANCING        = 6  # Claim-level financial balancing (stub)
    TRADING_PARTNER  = 7  # Payer-specific business rules (stub)


# ─────────────────────────────────────────────────────────────────────────────
# Result DTOs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SNIPViolation:
    tier: SNIPTier
    error_code: str          # Machine-readable code (e.g., "T1_MISSING_FIELD")
    field_path: str          # Dot-notation path to offending field
    message: str             # Human-readable description
    severity: str = "error"  # "error" | "warning"


@dataclass
class SNIPResult:
    claim_id: str
    passed: bool
    highest_tier_passed: SNIPTier | None
    failing_tier: SNIPTier | None
    violations: list[SNIPViolation] = field(default_factory=list)
    tier_timings_ms: dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "passed": self.passed,
            "highest_tier_passed": self.highest_tier_passed,
            "failing_tier": self.failing_tier,
            "violations": [
                {
                    "tier": v.tier,
                    "error_code": v.error_code,
                    "field_path": v.field_path,
                    "message": v.message,
                    "severity": v.severity,
                }
                for v in self.violations
            ],
        }


class SNIPValidationError(Exception):
    """Raised when any SNIP tier rejects the claim payload."""
    def __init__(self, result: SNIPResult) -> None:
        self.result = result
        tier = result.failing_tier
        codes = [v.error_code for v in result.violations]
        super().__init__(f"SNIP Tier {tier} failed: {codes}")


# ─────────────────────────────────────────────────────────────────────────────
# NPI Validation — Luhn Algorithm
# ─────────────────────────────────────────────────────────────────────────────

def _validate_npi_luhn(npi: str) -> bool:
    """
    Validate a 10-digit NPI using the ISO 7812 Luhn checksum algorithm.

    The NPI Luhn check prepends "80840" to the 10-digit NPI before
    applying the standard Luhn algorithm (per CMS NPI Final Rule).

    This catches transposed digits and random numeric strings that happen
    to be 10 digits — reducing provider identity fraud risk.
    """
    if not re.fullmatch(r"\d{10}", npi):
        return False
    # Prepend CMS prefix for NPI Luhn computation
    full = "80840" + npi
    digits = [int(d) for d in full]
    # Luhn: double every second digit from right
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tier Implementations
# ─────────────────────────────────────────────────────────────────────────────

def _tier1_integrity(payload: EDIClaimPayload) -> list[SNIPViolation]:
    """
    Tier 1 — Structural Integrity.

    Validates that all required fields are present and meet basic type
    and format constraints. Pydantic has already enforced Python types;
    this tier catches business-rule violations that Pydantic cannot express:
      • claim_number is not empty or whitespace-only
      • service_date_end is not before service_date_start
      • At least one procedure line exists
      • Total charge is strictly positive
    """
    violations: list[SNIPViolation] = []

    if not payload.interchange_control_number.strip():
        violations.append(SNIPViolation(
            tier=SNIPTier.INTEGRITY,
            error_code="T1_EMPTY_ICN",
            field_path="interchange_control_number",
            message="Interchange Control Number cannot be blank.",
        ))

    if payload.transaction_set not in {"837P", "837I", "837D"}:
        violations.append(SNIPViolation(
            tier=SNIPTier.INTEGRITY,
            error_code="T1_INVALID_TRANSACTION_SET",
            field_path="transaction_set",
            message=f"Unknown transaction set '{payload.transaction_set}'. Expected 837P/I/D.",
        ))

    if not payload.procedure_lines:
        violations.append(SNIPViolation(
            tier=SNIPTier.INTEGRITY,
            error_code="T1_NO_PROCEDURE_LINES",
            field_path="procedure_lines",
            message="Claim must contain at least one service line.",
        ))

    if payload.total_charge <= Decimal("0"):
        violations.append(SNIPViolation(
            tier=SNIPTier.INTEGRITY,
            error_code="T1_NONPOSITIVE_TOTAL",
            field_path="total_charge",
            message="Total claim charge must be greater than $0.00.",
        ))

    if (
        payload.service_date_end is not None
        and payload.service_date_end < payload.service_date_start
    ):
        violations.append(SNIPViolation(
            tier=SNIPTier.INTEGRITY,
            error_code="T1_DATE_ORDER",
            field_path="service_date_end",
            message="service_date_end cannot precede service_date_start.",
        ))

    for i, line in enumerate(payload.procedure_lines):
        if line.charge_amount <= Decimal("0"):
            violations.append(SNIPViolation(
                tier=SNIPTier.INTEGRITY,
                error_code="T1_LINE_NONPOSITIVE_CHARGE",
                field_path=f"procedure_lines[{i}].charge_amount",
                message=f"Line {line.line_number} has non-positive charge: {line.charge_amount}.",
            ))
        if line.units < 1:
            violations.append(SNIPViolation(
                tier=SNIPTier.INTEGRITY,
                error_code="T1_LINE_ZERO_UNITS",
                field_path=f"procedure_lines[{i}].units",
                message=f"Line {line.line_number} has zero or negative units.",
            ))

    return violations


def _tier2_hipaa_compliance(payload: EDIClaimPayload) -> list[SNIPViolation]:
    """
    Tier 2 — HIPAA Compliance.

    Enforces mandatory field presence and format per HIPAA Transaction
    Standards (45 CFR Part 162) and CMS NPI Final Rule (45 CFR Part 162.408).

    Key checks:
      • Billing provider NPI: required, 10 digits, passes Luhn checksum.
      • At least one ICD-10 diagnosis code must be present.
      • Diagnosis codes must match ICD-10-CM format ([A-Z]\d{2}\.?\w{0,4}).
      • Patient ID must be a valid UUID (already enforced by Pydantic).
    """
    violations: list[SNIPViolation] = []

    # Billing provider NPI — mandatory per HIPAA
    npi = payload.billing_provider_npi.strip()
    if not npi:
        violations.append(SNIPViolation(
            tier=SNIPTier.HIPAA_COMPLIANCE,
            error_code="T2_MISSING_BILLING_NPI",
            field_path="billing_provider_npi",
            message="Billing provider NPI is required per 45 CFR §162.408.",
        ))
    elif not _validate_npi_luhn(npi):
        violations.append(SNIPViolation(
            tier=SNIPTier.HIPAA_COMPLIANCE,
            error_code="T2_INVALID_NPI_LUHN",
            field_path="billing_provider_npi",
            message=f"NPI '{npi}' fails Luhn checksum validation (CMS NPI Final Rule).",
        ))

    # Rendering provider NPI — validate format if present
    if payload.rendering_provider_npi:
        if not _validate_npi_luhn(payload.rendering_provider_npi.strip()):
            violations.append(SNIPViolation(
                tier=SNIPTier.HIPAA_COMPLIANCE,
                error_code="T2_INVALID_RENDERING_NPI_LUHN",
                field_path="rendering_provider_npi",
                message="Rendering provider NPI fails Luhn checksum.",
            ))

    # ICD-10-CM diagnosis codes — at least one required
    if not payload.diagnosis_codes:
        violations.append(SNIPViolation(
            tier=SNIPTier.HIPAA_COMPLIANCE,
            error_code="T2_NO_DIAGNOSIS_CODES",
            field_path="diagnosis_codes",
            message="At least one ICD-10-CM diagnosis code is required (Loop 2300 HI).",
        ))
    else:
        # ICD-10-CM pattern: Letter + 2 digits + optional decimal + 1-4 alphanumeric
        ICD10_RE = re.compile(r"^[A-Z]\d{2}\.?\w{0,4}$", re.IGNORECASE)
        for i, code in enumerate(payload.diagnosis_codes):
            if not ICD10_RE.match(code.strip()):
                violations.append(SNIPViolation(
                    tier=SNIPTier.HIPAA_COMPLIANCE,
                    error_code="T2_INVALID_ICD10_FORMAT",
                    field_path=f"diagnosis_codes[{i}]",
                    message=f"'{code}' does not match ICD-10-CM format.",
                ))

    # CPT/HCPCS format check on each service line
    CPT_RE = re.compile(r"^\d{5}$|^[A-Z]\d{4}$", re.IGNORECASE)
    for i, line in enumerate(payload.procedure_lines):
        if not CPT_RE.match(line.procedure_code.strip()):
            violations.append(SNIPViolation(
                tier=SNIPTier.HIPAA_COMPLIANCE,
                error_code="T2_INVALID_CPT_FORMAT",
                field_path=f"procedure_lines[{i}].procedure_code",
                message=f"'{line.procedure_code}' is not a valid CPT/HCPCS code format.",
            ))

    return violations


def _tier3_balance_testing(payload: EDIClaimPayload) -> list[SNIPViolation]:
    """
    Tier 3 — Balance Testing.

    The sum of all service line charges MUST equal the header total_charge.
    This detects data corruption, EDI translation errors, and fraudulent
    header manipulation (e.g., inflating the header total after line items).

    DECIMAL ARITHMETIC IS MANDATORY.
    Floating-point arithmetic (IEEE 754) introduces rounding errors that
    accumulate across multiple line items. For a 50-line claim with amounts
    like $123.45, float arithmetic may compute $6,172.499999999... instead
    of $6,172.50, causing false positives. Using Python's `Decimal` type
    with ROUND_HALF_UP at 2 decimal places mirrors the precision used by
    clearinghouses and payers.

    TOLERANCE POLICY.
    We enforce strict equality ($0.00 tolerance). Some clearinghouses allow
    ±$0.01 for professional 837P claims. If future payer-specific rules
    require tolerance, implement it in Tier 7 (Trading Partner) to keep this
    general-purpose tier invariant.

    FAILURE HANDLING.
    A balance failure does NOT drop the claim. The caller (SNIPValidator.validate)
    catches SNIPValidationError and persists the claim as SNIP_REJECTED with
    the full SNIPResult embedded in ai_metadata. The submitter receives a
    structured 422 response with the exact computed vs. expected amounts.
    """
    violations: list[SNIPViolation] = []

    # Sum line charges using Decimal accumulation — no float conversion
    line_total = Decimal("0.00")
    for line in payload.procedure_lines:
        # Normalize each charge to 2 decimal places before accumulation
        normalized = line.charge_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        line_total += normalized

    header_total = payload.total_charge.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    line_total = line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if line_total != header_total:
        delta = abs(header_total - line_total)
        violations.append(SNIPViolation(
            tier=SNIPTier.BALANCE_TESTING,
            error_code="T3_BALANCE_MISMATCH",
            field_path="total_charge",
            message=(
                f"Claim balance test failed. "
                f"Header total_charge={header_total} does not equal "
                f"sum of line charges={line_total}. "
                f"Discrepancy: ${delta}. "
                f"This may indicate EDI translation error or payload tampering."
            ),
        ))

    return violations


def _tier4_inter_segment_stub(payload: EDIClaimPayload) -> list[SNIPViolation]:
    """
    Tier 4 — Inter-Segment Consistency (stub).

    Production implementation checks cross-field business rules:
      • Place of service code consistent with transaction set (837P vs 837I)
      • Service dates within policy effective/expiry period
      • Rendering provider NPI consistent with billing NPI taxonomy
    """
    return []  # Stub — extend per payer requirements


def _tier5_external_code_stub(payload: EDIClaimPayload) -> list[SNIPViolation]:
    """
    Tier 5 — External Code Set Validation (stub).

    Production implementation validates codes against live code tables:
      • ICD-10-CM codes against CMS ICD-10 tabular index
      • CPT codes against AMA CPT master file
      • HCPCS Level II codes against CMS HCPCS file
      • Place of service codes against CMS POS code list
    """
    return []


def _tier6_balancing_stub(payload: EDIClaimPayload) -> list[SNIPViolation]:
    """
    Tier 6 — Claim-Level Financial Balancing (stub).

    Validates that subscriber, patient, and payer financial segments
    balance to zero (EDI 837 Loop 2320 COB segments).
    """
    return []


def _tier7_trading_partner_stub(payload: EDIClaimPayload) -> list[SNIPViolation]:
    """
    Tier 7 — Trading Partner / Payer-Specific Rules (stub).

    Validates payer-specific business rules loaded from the payer's
    companion guide. Examples:
      • Blue Shield requires modifier 25 with E/M + procedure on same day
      • Medicare requires DX pointer linkage for each service line
    """
    return []


# ─────────────────────────────────────────────────────────────────────────────
# SNIP Validator — Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

_TIER_RUNNERS: list[tuple[SNIPTier, Any]] = [
    (SNIPTier.INTEGRITY,        _tier1_integrity),
    (SNIPTier.HIPAA_COMPLIANCE, _tier2_hipaa_compliance),
    (SNIPTier.BALANCE_TESTING,  _tier3_balance_testing),
    (SNIPTier.INTER_SEGMENT,    _tier4_inter_segment_stub),
    (SNIPTier.EXTERNAL_CODE,    _tier5_external_code_stub),
    (SNIPTier.BALANCING,        _tier6_balancing_stub),
    (SNIPTier.TRADING_PARTNER,  _tier7_trading_partner_stub),
]


async def validate_claim(
    payload: EDIClaimPayload,
    claim_id: str,
    stop_on_first_failure: bool = True,
) -> SNIPResult:
    """
    Execute the 7-tier SNIP validation pipeline against an EDI claim payload.

    Each tier is run in sequence. By default, the pipeline short-circuits on
    the first failing tier (mirroring clearinghouse behavior). Set
    stop_on_first_failure=False to collect all violations across all tiers
    (useful for batch re-validation and error reporting UIs).

    Tier functions are synchronous (pure computation). They run in the calling
    async context — no thread pool needed since there are no I/O operations.
    If future tiers require database lookups (e.g., Tier 5 code set validation),
    wrap those tiers with `asyncio.to_thread()`.

    Args:
        payload:               Parsed EDI claim payload.
        claim_id:              UUID string assigned to this claim.
        stop_on_first_failure: Short-circuit on first failing tier.

    Returns:
        SNIPResult with passed=True if all tiers succeed.

    Raises:
        SNIPValidationError: If any tier fails (contains full SNIPResult).
    """
    import time

    all_violations: list[SNIPViolation] = []
    highest_tier_passed: SNIPTier | None = None
    failing_tier: SNIPTier | None = None
    timings: dict[int, float] = {}

    for tier, runner in _TIER_RUNNERS:
        t0 = time.perf_counter()
        violations = runner(payload)
        timings[int(tier)] = (time.perf_counter() - t0) * 1000

        if violations:
            all_violations.extend(violations)
            failing_tier = tier
            logger.warning(
                "snip_tier_failed",
                claim_id=claim_id,
                tier=int(tier),
                tier_name=tier.name,
                violation_count=len(violations),
                codes=[v.error_code for v in violations],
            )
            if stop_on_first_failure:
                break
        else:
            highest_tier_passed = tier
            logger.debug("snip_tier_passed", claim_id=claim_id, tier=int(tier))

    passed = failing_tier is None
    result = SNIPResult(
        claim_id=claim_id,
        passed=passed,
        highest_tier_passed=highest_tier_passed,
        failing_tier=failing_tier,
        violations=all_violations,
        tier_timings_ms=timings,
    )

    if not passed:
        raise SNIPValidationError(result)

    logger.info(
        "snip_validation_passed",
        claim_id=claim_id,
        tiers_run=len(timings),
        total_ms=sum(timings.values()),
    )
    return result
