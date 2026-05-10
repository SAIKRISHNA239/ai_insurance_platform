"""
backend/api/routers/applications.py
─────────────────────────────────────
Underwriting applications HTTP router.

Endpoints:
  POST   /applications/                    — submit a new underwriting application
  GET    /applications/                    — list applications (role-scoped)
  GET    /applications/{app_id}            — get application detail
  PATCH  /applications/{app_id}/underwrite — underwriter decision + AI score
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user, get_db, require_role
from backend.database.models import (
    Application,
    ApplicationStatus,
    PolicyType,
    RiskTier,
    User,
    UserRole,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/applications", tags=["Applications (Underwriting)"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class ApplicationSubmitRequest(BaseModel):
    application_number: str = Field(max_length=64)
    policy_type: PolicyType
    requested_coverage_limit: Decimal = Field(gt=0)
    health_questionnaire: dict[str, Any] | None = None


class UnderwritingDecisionRequest(BaseModel):
    status: ApplicationStatus = Field(
        ...,
        description="Final underwriter decision status",
        examples=[ApplicationStatus.APPROVED, ApplicationStatus.DECLINED],
    )
    underwriting_score: float | None = Field(None, ge=0, le=100)
    risk_tier: RiskTier | None = None
    suggested_premium: Decimal | None = None
    ai_underwriting_notes: str | None = None
    decision_notes: str | None = None


class ApplicationResponse(BaseModel):
    id: uuid.UUID
    application_number: str
    applicant_id: uuid.UUID
    policy_type: PolicyType
    requested_coverage_limit: Decimal
    underwriting_score: float | None
    risk_tier: RiskTier | None
    status: ApplicationStatus
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedApplicationsResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ApplicationResponse]


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=ApplicationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a new insurance application",
)
async def submit_application(
    body: ApplicationSubmitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ApplicationResponse:
    existing = await db.execute(
        select(Application).where(
            Application.application_number == body.application_number
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An application with this number already exists.",
        )

    app = Application(
        application_number=body.application_number,
        applicant_id=current_user.id,
        policy_type=body.policy_type,
        requested_coverage_limit=body.requested_coverage_limit,
        health_questionnaire=body.health_questionnaire,
        status=ApplicationStatus.SUBMITTED,
    )
    db.add(app)
    await db.flush()

    logger.info(
        "application_submitted",
        app_id=str(app.id),
        user_id=str(current_user.id),
    )
    return ApplicationResponse.model_validate(app)


@router.get(
    "/",
    response_model=PaginatedApplicationsResponse,
    summary="List applications (paginated, role-scoped)",
)
async def list_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: ApplicationStatus | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaginatedApplicationsResponse:
    stmt = select(Application)

    if current_user.role == UserRole.INSURED:
        stmt = stmt.where(Application.applicant_id == current_user.id)

    if status_filter:
        stmt = stmt.where(Application.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.offset((page - 1) * page_size).limit(page_size).order_by(Application.created_at.desc())
    apps = (await db.execute(stmt)).scalars().all()

    return PaginatedApplicationsResponse(
        total=total, page=page, page_size=page_size,
        items=[ApplicationResponse.model_validate(a) for a in apps],
    )


@router.get(
    "/{app_id}",
    response_model=ApplicationResponse,
    summary="Get an application by ID",
)
async def get_application(
    app_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ApplicationResponse:
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()

    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    if current_user.role == UserRole.INSURED and app.applicant_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    return ApplicationResponse.model_validate(app)


@router.patch(
    "/{app_id}/underwrite",
    response_model=ApplicationResponse,
    summary="Record underwriting decision (underwriter/admin only)",
    dependencies=[Depends(require_role(UserRole.ADMIN, UserRole.UNDERWRITER))],
)
async def underwrite_application(
    app_id: uuid.UUID,
    body: UnderwritingDecisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ApplicationResponse:
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()

    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")

    if app.status not in {ApplicationStatus.SUBMITTED, ApplicationStatus.UNDER_REVIEW}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot underwrite application in status: {app.status}",
        )

    app.status = body.status
    app.underwriting_score = body.underwriting_score
    app.risk_tier = body.risk_tier
    app.suggested_premium = body.suggested_premium
    app.ai_underwriting_notes = body.ai_underwriting_notes
    app.decision_notes = body.decision_notes
    app.reviewed_by = current_user.id
    app.reviewed_at = datetime.utcnow()

    await db.flush()
    logger.info(
        "application_underwritten",
        app_id=str(app_id),
        status=body.status,
        underwriter=str(current_user.id),
    )
    return ApplicationResponse.model_validate(app)
