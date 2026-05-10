"""
backend/api/routers/claims.py  (v2 — EDA + SNIP rewrite)
──────────────────────────────────────────────────────────
Claims HTTP router with Event-Driven Architecture and SNIP validation.

ENDPOINT DESIGN: NON-BLOCKING INTAKE
──────────────────────────────────────
POST /claims/intake is the primary EDA entry point. It is designed to
return HTTP 202 Accepted in < 100ms regardless of claim complexity.

The endpoint does exactly three synchronous operations:
  1. Validate the inbound payload (Pydantic).
  2. Run SNIP validation (pure CPU, no I/O).
  3. Persist the claim row to PostgreSQL.
  4. Publish one event to Kafka.

It does NOT wait for:
  • UM routing
  • Fraud scoring
  • RAG pipeline
  • Payment calculation

These are downstream async consumers of the Kafka event.

The original CRUD endpoints (GET /claims/, GET /claims/{id}, PATCH status)
are preserved unchanged for compatibility with the existing API contract.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user, get_db, require_role
from backend.claims.schemas import EDIClaimPayload, EDIProcedureLine
from backend.claims.snip_validator import SNIPValidationError, validate_claim
from backend.claims.state_machine import (
    AdjudicationState,
    ClaimEvent,
    build_transition_record,
)
from backend.database.models import Claim, ClaimStatus, Policy, User, UserRole
from backend.workflows.events import (
    KafkaPublishError,
    UMRoutingDecision,
    process_validated_claim,
    publish_claim_validated,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/claims", tags=["Claims"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Schemas — EDA Intake
# ─────────────────────────────────────────────────────────────────────────────

class ProcedureLineRequest(BaseModel):
    """Single service line in the EDA claim intake payload."""
    line_number: int = Field(ge=1)
    procedure_code: str = Field(
        description="CPT or HCPCS Level II code (5 characters).",
        examples=["99213", "27447"],
    )
    modifier: str | None = Field(None, max_length=8)
    units: int = Field(ge=1, default=1)
    charge_amount: Decimal = Field(gt=Decimal("0"), description="Line charge in USD.")
    place_of_service: str | None = Field(None, max_length=5)
    rendering_provider_npi: str | None = Field(None, max_length=10)

    @field_validator("procedure_code")
    @classmethod
    def _strip_procedure_code(cls, v: str) -> str:
        return v.strip().upper()


class ClaimIntakeRequest(BaseModel):
    """
    EDA claim intake payload — simulates an EDI 837 to JSON conversion.

    Balance testing contract (Tier 3 SNIP):
        sum(procedure_lines[*].charge_amount) MUST equal total_charge.
        Violations cause HTTP 422 with structured SNIP error details.
        The claim is still persisted as SNIP_REJECTED for audit trail.
    """
    # EDI Envelope
    transaction_set: str = Field(
        default="837P",
        description="EDI transaction set: 837P (professional) or 837I (institutional).",
        examples=["837P", "837I"],
    )
    interchange_control_number: str = Field(
        max_length=20,
        description="EDI ISA13 interchange control number. Must be unique.",
    )
    group_control_number: str | None = Field(None, max_length=20)

    # Provider Identity
    billing_provider_npi: str = Field(
        max_length=10,
        description="10-digit NPI of billing provider. Validated with Luhn checksum.",
    )
    rendering_provider_npi: str | None = Field(None, max_length=10)

    # Policy Reference
    policy_id: uuid.UUID

    # Service Information
    service_date_start: date
    service_date_end: date | None = None
    place_of_service: str | None = Field(None, max_length=5, examples=["11", "21"])

    # Clinical Codes
    diagnosis_codes: list[str] = Field(
        min_length=1,
        description="ICD-10-CM diagnosis codes. At least one required (HIPAA Tier 2).",
    )

    # Service Lines
    procedure_lines: list[ProcedureLineRequest] = Field(min_length=1)

    # Financial Header
    total_charge: Decimal = Field(
        gt=Decimal("0"),
        description=(
            "Header total claim charge. "
            "MUST equal sum of all procedure_lines.charge_amount (SNIP Tier 3)."
        ),
    )


class ClaimIntakeResponse(BaseModel):
    """HTTP 202 response for EDA claim intake."""
    claim_id: str
    claim_number: str
    adjudication_state: str
    snip_status: str                    # "passed" | "rejected"
    snip_failing_tier: int | None       # None if passed
    snip_violations: list[dict]         # Empty if passed
    um_route: str | None                # "clinical_review" | "stp" | None
    um_triggers: list[str]
    message: str
    submitted_at: str


# ─────────────────────────────────────────────────────────────────────────────
# Legacy CRUD Schemas (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

class ClaimSubmitRequest(BaseModel):
    policy_id: uuid.UUID
    claim_number: str = Field(max_length=64)
    edi_transaction_set: str | None = Field(None, max_length=10)
    edi_interchange_control_number: str | None = Field(None, max_length=20)
    billing_provider_npi: str | None = Field(None, max_length=10)
    service_date_start: date
    service_date_end: date | None = None
    billed_amount: Decimal = Field(gt=0)
    diagnosis_codes: list[str] | None = None
    procedure_codes: list[dict[str, Any]] | None = None
    place_of_service: str | None = Field(None, max_length=5)


class ClaimStatusUpdateRequest(BaseModel):
    status: ClaimStatus
    denial_reason: str | None = None


class ClaimResponse(BaseModel):
    id: uuid.UUID
    claim_number: str
    policy_id: uuid.UUID
    claimant_id: uuid.UUID
    status: ClaimStatus
    billed_amount: Decimal
    allowed_amount: Decimal | None
    paid_amount: Decimal | None
    service_date_start: date
    fraud_score: float | None
    ai_notes: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


class PaginatedClaimsResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ClaimResponse]


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Convert intake request → EDIClaimPayload
# ─────────────────────────────────────────────────────────────────────────────

def _to_edi_payload(
    body: ClaimIntakeRequest,
    claimant_id: uuid.UUID,
) -> EDIClaimPayload:
    return EDIClaimPayload(
        transaction_set=body.transaction_set,
        interchange_control_number=body.interchange_control_number,
        group_control_number=body.group_control_number,
        billing_provider_npi=body.billing_provider_npi,
        rendering_provider_npi=body.rendering_provider_npi,
        patient_id=claimant_id,
        policy_id=body.policy_id,
        service_date_start=body.service_date_start,
        service_date_end=body.service_date_end,
        diagnosis_codes=body.diagnosis_codes,
        procedure_lines=[
            EDIProcedureLine(
                line_number=l.line_number,
                procedure_code=l.procedure_code,
                modifier=l.modifier,
                units=l.units,
                charge_amount=l.charge_amount,
                place_of_service=l.place_of_service,
                rendering_provider_npi=l.rendering_provider_npi,
            )
            for l in body.procedure_lines
        ],
        total_charge=body.total_charge,
        place_of_service=body.place_of_service,
    )


def _generate_claim_number(icn: str) -> str:
    """Generate a deterministic claim number from the interchange control number."""
    prefix = datetime.utcnow().strftime("%Y%m")
    suffix = icn[-8:].zfill(8)
    return f"CLM-{prefix}-{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# EDA Intake Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/intake",
    response_model=ClaimIntakeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="EDA claim intake — SNIP validation + Kafka publish",
    description=(
        "Non-blocking claim intake endpoint. Runs 7-tier SNIP validation, "
        "persists the claim, publishes to Kafka, and returns 202 immediately. "
        "UM routing and adjudication happen asynchronously downstream."
    ),
)
async def intake_claim(
    body: ClaimIntakeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClaimIntakeResponse:
    """
    EDA claim intake pipeline:
      1. Verify policy access.
      2. Convert to EDIClaimPayload DTO.
      3. Run 7-tier SNIP validation.
         → On failure: persist as SNIP_REJECTED, return 422 with details.
         → On pass: continue.
      4. Persist claim as RECEIVED → VALIDATED.
      5. Publish `claims.validated` Kafka event.
      6. Schedule UM routing as a background task.
      7. Return 202 Accepted immediately.
    """
    # ── 1. Policy access check ─────────────────────────────────────────────
    result = await db.execute(select(Policy).where(Policy.id == body.policy_id))
    policy = result.scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    if current_user.role == UserRole.INSURED and policy.holder_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied to this policy.")

    claim_number = _generate_claim_number(body.interchange_control_number)
    claim_id = str(uuid.uuid4())
    edi_payload = _to_edi_payload(body, current_user.id)

    # ── 2. SNIP Validation ────────────────────────────────────────────────
    snip_result_dict: dict[str, Any] = {}
    snip_failed = False
    snip_error_response: dict[str, Any] = {}

    try:
        snip_result = await validate_claim(edi_payload, claim_id)
        snip_result_dict = snip_result.to_dict()
    except SNIPValidationError as exc:
        snip_result = exc.result
        snip_result_dict = snip_result.to_dict()
        snip_failed = True
        logger.warning(
            "snip_rejection",
            claim_id=claim_id,
            tier=snip_result.failing_tier,
            violations=[v.error_code for v in snip_result.violations],
        )

    # ── 3. Persist claim (ALWAYS — even SNIP-rejected claims) ─────────────
    initial_status = ClaimStatus.DENIED if snip_failed else ClaimStatus.SUBMITTED

    # Map adjudication states to ORM ClaimStatus
    proc_codes_for_db = [
        {
            "line": l.line_number,
            "code": l.procedure_code,
            "modifier": l.modifier,
            "units": l.units,
            "charge": str(l.charge_amount),
        }
        for l in body.procedure_lines
    ]

    claim = Claim(
        id=uuid.UUID(claim_id),
        claim_number=claim_number,
        policy_id=body.policy_id,
        claimant_id=current_user.id,
        edi_transaction_set=body.transaction_set,
        edi_interchange_control_number=body.interchange_control_number,
        billing_provider_npi=body.billing_provider_npi,
        rendering_provider_npi=body.rendering_provider_npi,
        service_date_start=body.service_date_start,
        service_date_end=body.service_date_end,
        billed_amount=body.total_charge,
        diagnosis_codes=body.diagnosis_codes,
        procedure_codes=proc_codes_for_db,
        place_of_service=body.place_of_service,
        status=initial_status,
        ai_metadata={
            "adjudication_state": (
                AdjudicationState.SNIP_REJECTED.value
                if snip_failed
                else AdjudicationState.VALIDATED.value
            ),
            "snip_result": snip_result_dict,
        },
    )
    db.add(claim)
    await db.flush()

    # ── 4a. SNIP rejected — return 422 with full violation details ─────────
    if snip_failed:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "SNIP_VALIDATION_FAILED",
                "claim_id": claim_id,
                "claim_number": claim_number,
                "adjudication_state": AdjudicationState.SNIP_REJECTED.value,
                "failing_tier": snip_result.failing_tier,
                "violations": [
                    {
                        "tier": v.tier,
                        "error_code": v.error_code,
                        "field": v.field_path,
                        "message": v.message,
                    }
                    for v in snip_result.violations
                ],
                "message": (
                    "Claim rejected at SNIP validation. "
                    "Correct the indicated fields and re-submit as a new claim."
                ),
            },
        )

    # ── 4b. Build transition record RECEIVED → VALIDATED ──────────────────
    transition = build_transition_record(
        claim_id=claim_id,
        from_state=AdjudicationState.RECEIVED,
        event=ClaimEvent.SNIP_PASS,
        triggered_by="snip_validator",
        reason="All 7 SNIP tiers passed.",
        metadata=snip_result_dict,
    )

    # ── 5. Publish to Kafka ────────────────────────────────────────────────
    kafka_failed = False
    try:
        await publish_claim_validated(
            claim_id=claim_id,
            payload=edi_payload,
            snip_result_dict=snip_result_dict,
            transition_record=transition,
        )
    except KafkaPublishError as exc:
        kafka_failed = True
        logger.error(
            "kafka_publish_failed_non_fatal",
            claim_id=claim_id,
            error=str(exc),
            msg="Claim persisted. Dead-letter retry will re-publish.",
        )
        # Write dead-letter marker into ai_metadata
        claim.ai_metadata = {
            **claim.ai_metadata,
            "kafka_publish_failed": True,
            "kafka_error": str(exc),
        }

    await db.commit()

    # ── 6. UM routing as background task (non-blocking) ───────────────────
    um_route: str | None = None
    um_triggers: list[str] = []

    background_tasks.add_task(
        _background_um_routing, claim_id, edi_payload
    )

    logger.info(
        "claim_intake_accepted",
        claim_id=claim_id,
        claim_number=claim_number,
        kafka_ok=not kafka_failed,
    )

    return ClaimIntakeResponse(
        claim_id=claim_id,
        claim_number=claim_number,
        adjudication_state=AdjudicationState.VALIDATED.value,
        snip_status="passed",
        snip_failing_tier=None,
        snip_violations=[],
        um_route=None,  # UM runs in background
        um_triggers=[],
        message=(
            "Claim accepted and published for adjudication. "
            "UM routing is processing asynchronously."
        ),
        submitted_at=datetime.utcnow().isoformat(),
    )


async def _background_um_routing(
    claim_id: str,
    payload: EDIClaimPayload,
) -> None:
    """
    Background task: execute UM routing after the HTTP response is sent.

    In production this logic moves to a dedicated Kafka consumer worker.
    For the current phase, FastAPI BackgroundTasks provides immediate
    async execution without blocking the HTTP response.
    """
    try:
        transition, routing = await process_validated_claim(claim_id, payload)
        logger.info(
            "background_um_complete",
            claim_id=claim_id,
            new_state=transition.to_state.value,
            route=routing.route,
        )
    except Exception as exc:
        logger.error(
            "background_um_failed",
            claim_id=claim_id,
            error=str(exc),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy CRUD Endpoints (preserved for API compatibility)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=ClaimResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a claim (legacy CRUD — no SNIP/EDA)",
)
async def submit_claim(
    body: ClaimSubmitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClaimResponse:
    result = await db.execute(select(Policy).where(Policy.id == body.policy_id))
    policy = result.scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    if current_user.role == UserRole.INSURED and policy.holder_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied.")

    claim = Claim(
        claim_number=body.claim_number,
        policy_id=body.policy_id,
        claimant_id=current_user.id,
        edi_transaction_set=body.edi_transaction_set,
        edi_interchange_control_number=body.edi_interchange_control_number,
        billing_provider_npi=body.billing_provider_npi,
        service_date_start=body.service_date_start,
        service_date_end=body.service_date_end,
        billed_amount=body.billed_amount,
        diagnosis_codes=body.diagnosis_codes,
        procedure_codes=body.procedure_codes,
        place_of_service=body.place_of_service,
    )
    db.add(claim)
    await db.flush()
    logger.info("legacy_claim_submitted", claim_id=str(claim.id))
    return ClaimResponse.model_validate(claim)


@router.get("/", response_model=PaginatedClaimsResponse, summary="List claims")
async def list_claims(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: ClaimStatus | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaginatedClaimsResponse:
    stmt = select(Claim)
    if current_user.role == UserRole.INSURED:
        stmt = stmt.where(Claim.claimant_id == current_user.id)
    if status_filter:
        stmt = stmt.where(Claim.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.offset((page - 1) * page_size).limit(page_size).order_by(Claim.created_at.desc())
    claims = (await db.execute(stmt)).scalars().all()

    return PaginatedClaimsResponse(
        total=total, page=page, page_size=page_size,
        items=[ClaimResponse.model_validate(c) for c in claims],
    )


@router.get("/{claim_id}", response_model=ClaimResponse, summary="Get claim by ID")
async def get_claim(
    claim_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClaimResponse:
    claim = (await db.execute(select(Claim).where(Claim.id == claim_id))).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found.")
    if current_user.role == UserRole.INSURED and claim.claimant_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied.")
    return ClaimResponse.model_validate(claim)


@router.patch(
    "/{claim_id}/status",
    response_model=ClaimResponse,
    summary="Update claim status",
    dependencies=[Depends(require_role(UserRole.ADMIN, UserRole.CLAIMS_ADJUSTER))],
)
async def update_claim_status(
    claim_id: uuid.UUID,
    body: ClaimStatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClaimResponse:
    claim = (await db.execute(select(Claim).where(Claim.id == claim_id))).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found.")

    claim.status = body.status
    if body.denial_reason:
        claim.denial_reason = body.denial_reason
    claim.adjudicated_by = current_user.id
    claim.adjudicated_at = datetime.utcnow()
    await db.flush()
    logger.info("claim_status_updated", claim_id=str(claim_id), status=body.status)
    return ClaimResponse.model_validate(claim)
