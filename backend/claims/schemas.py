"""
backend/claims/schemas.py
──────────────────────────
Pydantic schemas specific to the claims domain (not HTTP layer).

These are internal data transfer objects (DTOs) used between the
claims service, EDI parser, and the fraud scoring pipeline.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
import uuid

from pydantic import BaseModel, Field


class EDIProcedureLine(BaseModel):
    """Represents a single service line from EDI Loop 2400."""
    line_number: int
    procedure_code: str = Field(description="CPT or HCPCS code")
    modifier: str | None = None
    units: int = Field(ge=1, default=1)
    charge_amount: Decimal
    place_of_service: str | None = None
    rendering_provider_npi: str | None = None


class EDIClaimPayload(BaseModel):
    """
    Structured representation of an EDI 837 claim after parsing.
    Used internally by the EDI parsing pipeline before DB persistence.
    """
    transaction_set: str = Field(examples=["837P", "837I"])
    interchange_control_number: str
    group_control_number: str | None = None
    billing_provider_npi: str
    rendering_provider_npi: str | None = None
    patient_id: uuid.UUID
    policy_id: uuid.UUID
    service_date_start: date
    service_date_end: date | None = None
    diagnosis_codes: list[str]
    procedure_lines: list[EDIProcedureLine]
    total_charge: Decimal
    place_of_service: str | None = None
    raw_edi: str | None = Field(None, description="Original EDI 837 string for audit trail")


class FraudScoringInput(BaseModel):
    """Input features for the fraud ML pipeline."""
    claim_id: uuid.UUID
    billing_provider_npi: str | None
    billed_amount: Decimal
    diagnosis_codes: list[str]
    procedure_codes: list[dict[str, Any]]
    historical_claim_count: int = 0
    days_since_policy_start: int | None = None
