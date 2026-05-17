"""
backend/api/routers/system.py
──────────────────────────────
High-performance system-level aggregates endpoint.

Endpoints:
  GET /system/stats — returns tenant-scoped COUNT() aggregates for the
                      dashboard KPI cards.

Design:
  • Uses SQLAlchemy func.count() sub-queries — never fetches full rows.
  • All counts are filtered by tenant_id (multi-tenant isolation).
  • INSURED users see only their own claims / applications.
  • Response in < 5 ms on a normally loaded Postgres instance.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user, get_db
from backend.database.models import (
    Application,
    ApplicationStatus,
    Claim,
    ClaimStatus,
    User,
    UserRole,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/system", tags=["System"])


# ── Schema ─────────────────────────────────────────────────────────────────────

class SystemStatsResponse(BaseModel):
    total_claims: int
    total_applications: int
    approved_claims: int
    pending_applications: int


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.get(
    "/stats",
    response_model=SystemStatsResponse,
    summary="High-performance dashboard KPI aggregates",
    description=(
        "Returns tenant-scoped COUNT() aggregates via SQL — no full rows fetched. "
        "Suitable for polling at high frequency (e.g. dashboard refresh)."
    ),
)
async def get_system_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SystemStatsResponse:
    is_insured = current_user.role == UserRole.INSURED

    # ── Claims base filter ─────────────────────────────────────────────────────
    claims_base = select(func.count()).select_from(Claim).where(
        Claim.tenant_id == current_user.tenant_id
    )
    if is_insured:
        claims_base = claims_base.where(Claim.claimant_id == current_user.id)

    # ── Applications base filter ───────────────────────────────────────────────
    apps_base = select(func.count()).select_from(Application).where(
        Application.tenant_id == current_user.tenant_id
    )
    if is_insured:
        apps_base = apps_base.where(Application.applicant_id == current_user.id)

    # ── Execute all four counts ────────────────────────────────────────────────
    r_total_claims        = await db.execute(claims_base)
    r_total_applications  = await db.execute(apps_base)
    r_approved_claims     = await db.execute(
        claims_base.where(Claim.status == ClaimStatus.APPROVED)
    )
    r_pending_applications = await db.execute(
        apps_base.where(Application.status == ApplicationStatus.UNDER_REVIEW)
    )

    result = SystemStatsResponse(
        total_claims=r_total_claims.scalar_one(),
        total_applications=r_total_applications.scalar_one(),
        approved_claims=r_approved_claims.scalar_one(),
        pending_applications=r_pending_applications.scalar_one(),
    )

    logger.debug(
        "system_stats_served",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
        **result.model_dump(),
    )
    return result
