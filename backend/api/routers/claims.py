"""
backend/api/routers/claims.py  (v3 — EDA + SNIP + File Upload)
----------------------------------------------------------------
Claims HTTP router with Event-Driven Architecture and SNIP validation.

ENDPOINT DESIGN: NON-BLOCKING INTAKE
--------------------------------------
POST /claims/intake  — JSON EDI 837 payload
POST /claims/upload  — multipart PDF/PNG/JPG with mock EDI extraction
Both return HTTP 202 Accepted in < 100ms.

The shared _run_eda_intake() helper contains the full SNIP + Kafka +
adjudication pipeline used by both intake endpoints, ensuring identical
behaviour regardless of input format.

Legacy CRUD endpoints are preserved unchanged for API compatibility.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
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
    process_validated_claim,
    publish_claim_validated,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/claims", tags=["Claims"])


# -----------------------------------------------------------------------------
# Pydantic Schemas — EDA Intake
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Legacy CRUD Schemas (unchanged)
# -----------------------------------------------------------------------------

class ClaimSubmitRequest(BaseModel):
    policy_id: uuid.UUID
    claim_number: str = Field(max_length=64)
    edi_transaction_set: str | None = Field(None, max_length=10)
    edi_interchange_control_number: str | None = Field(None, max_length=20)
    billing_provider_npi: str | None = Field(None, max_length=10)
    service_date_start: date
    service_date_end: date | None = None
    billed_amount: Decimal = Field(gt=Decimal("0"))
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


# -----------------------------------------------------------------------------
# Helper: Convert intake request to EDIClaimPayload DTO
# -----------------------------------------------------------------------------

def _to_edi_payload(body: ClaimIntakeRequest, claimant_id: uuid.UUID) -> EDIClaimPayload:
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
                line_number=ln.line_number,
                procedure_code=ln.procedure_code,
                modifier=ln.modifier,
                units=ln.units,
                charge_amount=ln.charge_amount,
                place_of_service=ln.place_of_service,
                rendering_provider_npi=ln.rendering_provider_npi,
            )
            for ln in body.procedure_lines
        ],
        total_charge=body.total_charge,
        place_of_service=body.place_of_service,
    )


def _generate_claim_number(icn: str) -> str:
    """Generate a deterministic claim number from the interchange control number."""
    prefix = datetime.now(timezone.utc).strftime("%Y%m")
    suffix = icn[-8:].zfill(8)
    return f"CLM-{prefix}-{suffix}"


# -----------------------------------------------------------------------------
# Shared EDA Intake Service
# -----------------------------------------------------------------------------

async def _run_eda_intake(
    body: ClaimIntakeRequest,
    db: AsyncSession,
    current_user: User,
    background_tasks: BackgroundTasks,
) -> ClaimIntakeResponse:
    """
    Core EDA intake pipeline shared by POST /claims/intake and POST /claims/upload.

    Steps:
      1. Convert ClaimIntakeRequest -> EDIClaimPayload DTO.
      2. Run 7-tier SNIP validation.
      3. Persist claim to PostgreSQL (always, even SNIP-rejected).
      4. On SNIP fail: raise HTTP 422 with structured violation details.
      5. Publish claims.validated Kafka event (non-fatal failure).
      6. Schedule UM routing as background task.
      7. Return 202 ClaimIntakeResponse.
    """
    claim_number = _generate_claim_number(body.interchange_control_number)
    claim_id = str(uuid.uuid4())
    edi_payload = _to_edi_payload(body, current_user.id)

    # SNIP Validation
    snip_result_dict: dict[str, Any] = {}
    snip_failed = False
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

    # Persist claim (always — even SNIP-rejected claims)
    initial_status = ClaimStatus.DENIED if snip_failed else ClaimStatus.SUBMITTED

    proc_codes_for_db = [
        {
            "line": ln.line_number,
            "code": ln.procedure_code,
            "modifier": ln.modifier,
            "units": ln.units,
            "charge": str(ln.charge_amount),
        }
        for ln in body.procedure_lines
    ]

    claim = Claim(
        id=uuid.UUID(claim_id),
        claim_number=claim_number,
        policy_id=body.policy_id,
        claimant_id=current_user.id,
        tenant_id=current_user.tenant_id,
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
        raw_edi_payload={
            "transaction_set": body.transaction_set,
            "interchange_control_number": body.interchange_control_number,
            "billing_provider_npi": body.billing_provider_npi,
            "total_charge": str(body.total_charge),
            "procedure_line_count": len(body.procedure_lines),
            "diagnosis_code_count": len(body.diagnosis_codes),
        },
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

    # SNIP rejected — return 422 with full violation details
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

    # Build transition record RECEIVED -> VALIDATED
    transition = build_transition_record(
        claim_id=claim_id,
        from_state=AdjudicationState.RECEIVED,
        event=ClaimEvent.SNIP_PASS,
        triggered_by="snip_validator",
        reason="All 7 SNIP tiers passed.",
        metadata=snip_result_dict,
    )

    # Publish to Kafka (non-fatal on failure)
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
        existing_meta = claim.ai_metadata or {}
        claim.ai_metadata = {
            **existing_meta,
            "kafka_publish_failed": True,
            "kafka_error": str(exc),
            "kafka_failed_at": datetime.now(timezone.utc).isoformat(),
        }

    await db.commit()
    background_tasks.add_task(_background_um_routing, claim_id, edi_payload)

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
        um_route=None,
        um_triggers=[],
        message=(
            "Claim accepted and published for adjudication. "
            "UM routing is processing asynchronously."
        ),
        submitted_at=datetime.now(timezone.utc).isoformat(),
    )


async def _background_um_routing(claim_id: str, payload: EDIClaimPayload) -> None:
    """Background task: execute UM routing after the HTTP response is sent."""
    try:
        transition, routing = await process_validated_claim(claim_id, payload)
        logger.info(
            "background_um_complete",
            claim_id=claim_id,
            new_state=transition.to_state.value,
            route=routing.route,
        )
    except Exception as exc:
        logger.error("background_um_failed", claim_id=claim_id, error=str(exc))


# -----------------------------------------------------------------------------
# EDA Intake Endpoint — JSON (POST /claims/intake)
# -----------------------------------------------------------------------------

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
    # Verify policy access
    result = await db.execute(select(Policy).where(Policy.id == body.policy_id))
    policy = result.scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    if current_user.role == UserRole.INSURED and policy.holder_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied to this policy.")

    return await _run_eda_intake(body, db, current_user, background_tasks)


# -----------------------------------------------------------------------------
# File Upload Endpoint — PDF/PNG/JPG (POST /claims/upload)
# -----------------------------------------------------------------------------

# NPI that passes the CMS Luhn checksum:
# full = "80840" + "1234567893" = "808401234567893"
# Luhn sum = 70 -> 70 % 10 == 0 -> valid
_MOCK_BILLING_NPI       = "1234567893"
_MOCK_DIAGNOSIS_CODES   = ["Z00.00"]   # Encounter for general adult examination
_MOCK_CPT_CODE          = "99213"      # Office visit, established patient, low complexity
_MOCK_CHARGE            = Decimal("250.00")
_ALLOWED_UPLOAD_TYPES   = {"application/pdf", "image/png", "image/jpeg", "image/jpg"}


def _build_mock_claim_request(policy_id: uuid.UUID, filename: str) -> ClaimIntakeRequest:
    """
    Generate a ClaimIntakeRequest from uploaded file metadata.

    Uses a SHA-256 hash of the filename as the interchange_control_number seed,
    so different filenames produce distinct claim numbers while the same file
    is idempotent.  The generated payload is guaranteed to pass all 7 SNIP tiers:
      - Tier 1: non-empty ICN, valid transaction_set, positive charge, 1+ line
      - Tier 2: NPI passes Luhn, valid ICD-10 Z00.00, valid CPT 99213
      - Tier 3: total_charge == sum(line charges) exactly (single line)
      - Tiers 4-7: stub implementations — always pass

    In production this step would call a document-intelligence service (e.g.
    Google Document AI or Azure Form Recognizer) to extract real EDI fields.
    """
    icn = hashlib.sha256(filename.encode()).hexdigest()[:9].upper()

    return ClaimIntakeRequest(
        transaction_set="837P",
        interchange_control_number=icn,
        group_control_number=None,
        billing_provider_npi=_MOCK_BILLING_NPI,
        rendering_provider_npi=None,
        policy_id=policy_id,
        service_date_start=date.today(),
        service_date_end=None,
        place_of_service="11",           # Office
        diagnosis_codes=_MOCK_DIAGNOSIS_CODES,
        procedure_lines=[
            ProcedureLineRequest(
                line_number=1,
                procedure_code=_MOCK_CPT_CODE,
                modifier=None,
                units=1,
                charge_amount=_MOCK_CHARGE,
                place_of_service="11",
            )
        ],
        total_charge=_MOCK_CHARGE,       # Exact match -> Tier 3 passes
    )


@router.post(
    "/upload",
    response_model=ClaimIntakeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload claim document (PDF/PNG/JPG) — mock EDI extraction + EDA intake",
    description=(
        "Accepts a multipart/form-data file upload (PDF, PNG, or JPG). "
        "Performs mock EDI 837 extraction (real OCR in production), then runs "
        "the full SNIP validation, Kafka publish, and UM routing pipeline. "
        "Returns 202 Accepted immediately; UM routing runs asynchronously."
    ),
)
async def upload_claim(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Claim document: PDF, PNG, or JPG"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClaimIntakeResponse:
    # Validate content type
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{content_type}'. "
                "Accepted: application/pdf, image/png, image/jpeg."
            ),
        )

    # Resolve a policy_id for this tenant's user.
    # In production the caller would pass an explicit policy_id.
    policy_stmt = (
        select(Policy)
        .where(Policy.tenant_id == current_user.tenant_id)
        .limit(1)
    )
    if current_user.role == UserRole.INSURED:
        policy_stmt = policy_stmt.where(Policy.holder_id == current_user.id)

    policy_row = (await db.execute(policy_stmt)).scalar_one_or_none()
    if policy_row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No policy found for your account. "
                "A policy must exist before a claim can be submitted."
            ),
        )

    filename = file.filename or "uploaded_claim"
    mock_body = _build_mock_claim_request(policy_row.id, filename)

    logger.info(
        "claim_upload_received",
        filename=filename,
        content_type=content_type,
        policy_id=str(policy_row.id),
        user_id=str(current_user.id),
    )

    return await _run_eda_intake(mock_body, db, current_user, background_tasks)


# -----------------------------------------------------------------------------
# Legacy CRUD Endpoints (preserved for API compatibility)
# -----------------------------------------------------------------------------

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
        tenant_id=current_user.tenant_id,
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
    stmt = select(Claim).where(Claim.tenant_id == current_user.tenant_id)
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
    claim = (await db.execute(
        select(Claim).where(
            Claim.id == claim_id,
            Claim.tenant_id == current_user.tenant_id,
        )
    )).scalar_one_or_none()
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
    claim = (await db.execute(
        select(Claim).where(
            Claim.id == claim_id,
            Claim.tenant_id == current_user.tenant_id,
        )
    )).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found.")

    claim.status = body.status
    if body.denial_reason:
        claim.denial_reason = body.denial_reason
    claim.adjudicated_by = current_user.id
    claim.adjudicated_at = datetime.now(timezone.utc)
    await db.flush()
    logger.info("claim_status_updated", claim_id=str(claim_id), status=body.status)
    return ClaimResponse.model_validate(claim)
