"""
backend/policies/schemas.py
────────────────────────────
Internal Pydantic schemas for the policies domain.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Any
import uuid
from pydantic import BaseModel
from backend.database.models import PolicyStatus, PolicyType


class PolicySummary(BaseModel):
    id: uuid.UUID
    policy_number: str
    policy_type: PolicyType
    status: PolicyStatus
    effective_date: date
    expiry_date: date
    premium_amount: Decimal

    model_config = {"from_attributes": True}
