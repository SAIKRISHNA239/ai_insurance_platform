"""
backend/api/routers/policies.py
────────────────────────────────
Policies HTTP router.

Endpoints:
  POST /policies/            — create a policy (admin/underwriter)
  GET  /policies/            — list policies (scoped by role)
  GET  /policies/{policy_id} — get policy detail
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user, get_db, require_role
from backend.database.models import Policy, PolicyStatus, PolicyType, User, UserRole

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/policies", tags=["Policies"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class PolicyCreateRequest(BaseModel):
    policy_number: str = Field(max_length=64)
    holder_id: uuid.UUID
    policy_type: PolicyType
    premium_amount: Decimal = Field(gt=0)
    coverage_limit: Decimal = Field(gt=0)
    deductible: Decimal = Field(ge=0, default=Decimal("0.00"))
    out_of_pocket_max: Decimal | None = None
    effective_date: date
    expiry_date: date
    benefits_schedule: dict[str, Any] | None = None


class PolicyResponse(BaseModel):
    id: uuid.UUID
    policy_number: str
    holder_id: uuid.UUID
    policy_type: PolicyType
    premium_amount: Decimal
    coverage_limit: Decimal
    deductible: Decimal
    effective_date: date
    expiry_date: date
    status: PolicyStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedPoliciesResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PolicyResponse]


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=PolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new insurance policy (admin/underwriter only)",
    dependencies=[Depends(require_role(UserRole.ADMIN, UserRole.UNDERWRITER))],
)
async def create_policy(
    body: PolicyCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PolicyResponse:
    if body.effective_date >= body.expiry_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="effective_date must be before expiry_date.",
        )

    # Check for duplicate policy_number
    existing = await db.execute(
        select(Policy).where(Policy.policy_number == body.policy_number)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A policy with this number already exists.",
        )

    policy = Policy(**body.model_dump())
    db.add(policy)
    await db.flush()

    logger.info("policy_created", policy_id=str(policy.id), number=policy.policy_number)
    return PolicyResponse.model_validate(policy)


@router.get(
    "/",
    response_model=PaginatedPoliciesResponse,
    summary="List policies (paginated, role-scoped)",
)
async def list_policies(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: PolicyStatus | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaginatedPoliciesResponse:
    stmt = select(Policy)

    if current_user.role == UserRole.INSURED:
        stmt = stmt.where(Policy.holder_id == current_user.id)

    if status_filter:
        stmt = stmt.where(Policy.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.offset((page - 1) * page_size).limit(page_size).order_by(Policy.created_at.desc())
    policies = (await db.execute(stmt)).scalars().all()

    return PaginatedPoliciesResponse(
        total=total, page=page, page_size=page_size,
        items=[PolicyResponse.model_validate(p) for p in policies],
    )


@router.get(
    "/{policy_id}",
    response_model=PolicyResponse,
    summary="Get a specific policy by ID",
)
async def get_policy(
    policy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PolicyResponse:
    result = await db.execute(select(Policy).where(Policy.id == policy_id))
    policy = result.scalar_one_or_none()

    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found.")

    if current_user.role == UserRole.INSURED and policy.holder_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    return PolicyResponse.model_validate(policy)
