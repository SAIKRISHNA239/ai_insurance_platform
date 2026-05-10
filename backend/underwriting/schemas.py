"""
backend/underwriting/schemas.py
────────────────────────────────
Internal Pydantic schemas for the underwriting domain.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Any
import uuid
from pydantic import BaseModel, Field
from backend.database.models import RiskTier


class UnderwritingInput(BaseModel):
    application_id: uuid.UUID
    policy_type: str
    requested_coverage_limit: Decimal
    health_questionnaire: dict[str, Any] | None = None
    applicant_age: int | None = None


class UnderwritingOutput(BaseModel):
    underwriting_score: float = Field(ge=0, le=100)
    risk_tier: RiskTier
    suggested_premium: Decimal | None = None
    ai_notes: str | None = None
    reasoning_evidence: list[str] = []
