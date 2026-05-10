"""
backend/database/models.py
──────────────────────────
SQLAlchemy ORM models for the core insurance platform domain.

Schema design notes:
• All PKs are server-generated UUIDs (gen_random_uuid()) for security and
  distributed-system compatibility.
• Enum types are PostgreSQL native ENUMs for DB-level integrity.
• JSONB columns (diagnosis_codes, procedure_codes, health_questionnaire)
  allow flexible, schema-less document storage with full GIN index support.
• Audit columns (created_at, updated_at) use server_default / onupdate so
  the DB enforces timestamps even for bulk inserts outside the ORM.
• All relationships are lazy="selectin" for async safety — avoids implicit
  lazy-loading which is unsupported in async SQLAlchemy sessions.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations — mirrored as PostgreSQL native ENUM types
# ─────────────────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    ADMIN = "admin"
    UNDERWRITER = "underwriter"
    CLAIMS_ADJUSTER = "claims_adjuster"
    INSURED = "insured"


class PolicyType(str, enum.Enum):
    INDIVIDUAL = "individual"
    GROUP = "group"
    MEDICARE_SUPPLEMENT = "medicare_supplement"
    DENTAL = "dental"
    VISION = "vision"


class PolicyStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    LAPSED = "lapsed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ClaimStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    PENDING_INFO = "pending_info"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    DENIED = "denied"
    APPEALED = "appealed"
    CLOSED = "closed"


class ApplicationStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class RiskTier(str, enum.Enum):
    PREFERRED = "preferred"
    STANDARD = "standard"
    SUBSTANDARD = "substandard"
    DECLINE = "decline"


# ─────────────────────────────────────────────────────────────────────────────
# Mixin — shared audit columns
# ─────────────────────────────────────────────────────────────────────────────

class TimestampMixin:
    """Adds created_at and updated_at to any model."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────

class User(TimestampMixin, Base):
    """
    Platform user — represents every actor: admins, underwriters,
    claims adjusters, and insured members.
    """
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(1024), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", create_type=True),
        nullable=False,
        default=UserRole.INSURED,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Relationships ──────────────────────────────────────────────────────
    policies: Mapped[list["Policy"]] = relationship(
        "Policy",
        back_populates="holder",
        foreign_keys="[Policy.holder_id]",
        lazy="selectin",
    )
    claims: Mapped[list["Claim"]] = relationship(
        "Claim",
        back_populates="claimant",
        foreign_keys="[Claim.claimant_id]",
        lazy="selectin",
    )
    applications: Mapped[list["Application"]] = relationship(
        "Application",
        back_populates="applicant",
        foreign_keys="[Application.applicant_id]",
        lazy="selectin",
    )

    # ── Composite indexes ─────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_users_email_active", "email", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"


# ─────────────────────────────────────────────────────────────────────────────
# Policies
# ─────────────────────────────────────────────────────────────────────────────

class Policy(TimestampMixin, Base):
    """
    Insurance policy held by a User (insured member).
    A policy is the contract that backs individual claims.
    """
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    policy_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
        comment="Human-readable unique policy identifier",
    )
    holder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    policy_type: Mapped[PolicyType] = mapped_column(
        Enum(PolicyType, name="policy_type", create_type=True),
        nullable=False,
    )
    premium_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False,
        comment="Monthly premium in USD",
    )
    coverage_limit: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False,
        comment="Maximum annual benefit in USD",
    )
    deductible: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"),
        comment="Annual deductible before coverage kicks in",
    )
    out_of_pocket_max: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True,
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status", create_type=True),
        nullable=False,
        default=PolicyStatus.PENDING,
    )
    # Structured metadata stored as JSONB for flexible benefit schedules
    benefits_schedule: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ── Relationships ──────────────────────────────────────────────────────
    holder: Mapped["User"] = relationship(
        "User",
        back_populates="policies",
        foreign_keys=[holder_id],
        lazy="selectin",
    )
    claims: Mapped[list["Claim"]] = relationship(
        "Claim",
        back_populates="policy",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_policies_holder_status", "holder_id", "status"),
        Index("ix_policies_effective_expiry", "effective_date", "expiry_date"),
    )

    def __repr__(self) -> str:
        return f"<Policy id={self.id} number={self.policy_number} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# Claims
# ─────────────────────────────────────────────────────────────────────────────

class Claim(TimestampMixin, Base):
    """
    Healthcare insurance claim.

    EDI 837 (Professional/Institutional) key fields are captured as dedicated
    columns for structured queries and reporting.  Full raw EDI payload and
    AI inference results are stored as JSONB.
    """
    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    claim_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    claimant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # ── EDI 837 Structured Fields ──────────────────────────────────────────
    edi_transaction_set: Mapped[str | None] = mapped_column(
        String(10), nullable=True,
        comment="EDI transaction set identifier: 837P (professional) or 837I (institutional)",
    )
    edi_interchange_control_number: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True,
        comment="ISA13 — unique interchange control number from the EDI envelope",
    )
    edi_group_control_number: Mapped[str | None] = mapped_column(
        String(20), nullable=True,
        comment="GS06 — functional group control number",
    )
    billing_provider_npi: Mapped[str | None] = mapped_column(
        String(10), nullable=True, index=True,
        comment="10-digit National Provider Identifier of the billing provider",
    )
    rendering_provider_npi: Mapped[str | None] = mapped_column(
        String(10), nullable=True,
        comment="NPI of the actual rendering clinician",
    )
    facility_npi: Mapped[str | None] = mapped_column(
        String(10), nullable=True,
    )
    service_date_start: Mapped[date] = mapped_column(Date, nullable=False)
    service_date_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    # ── Financial ──────────────────────────────────────────────────────────
    billed_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    allowed_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    paid_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    member_responsibility: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True,
        comment="Deductible + copay + coinsurance owed by member",
    )

    # ── Clinical Codes — JSONB for multi-code support ──────────────────────
    # Example: ["Z51.11", "C34.12"]
    diagnosis_codes: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True,
        comment="ICD-10-CM diagnosis codes from EDI Loop 2300 HI segment",
    )
    # Example: [{"code": "99213", "modifier": "25", "units": 1, "charge": 150.00}]
    procedure_codes: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True,
        comment="CPT/HCPCS procedure line items from EDI Loop 2400 SV1 segment",
    )
    # Place of service code (e.g., "11" = Office, "21" = Inpatient Hospital)
    place_of_service: Mapped[str | None] = mapped_column(String(5), nullable=True)

    # ── AI / ML Inference Results ──────────────────────────────────────────
    fraud_score: Mapped[float | None] = mapped_column(
        Numeric(5, 4), nullable=True,
        comment="Fraud probability [0.0–1.0] from ML model",
    )
    ai_notes: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="LLM-generated adjudication notes and recommendations",
    )
    ai_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True,
        comment="Structured AI inference outputs: model name, confidence, evidence chunks",
    )

    # ── Status & Workflow ──────────────────────────────────────────────────
    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus, name="claim_status", create_type=True),
        nullable=False,
        default=ClaimStatus.SUBMITTED,
    )
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjudicated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    adjudicated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # ── Relationships ──────────────────────────────────────────────────────
    policy: Mapped["Policy"] = relationship(
        "Policy", back_populates="claims", lazy="selectin",
    )
    claimant: Mapped["User"] = relationship(
        "User",
        back_populates="claims",
        foreign_keys=[claimant_id],
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_claims_policy_status", "policy_id", "status"),
        Index("ix_claims_claimant_status", "claimant_id", "status"),
        Index("ix_claims_service_date", "service_date_start"),
        # GIN index for fast JSONB queries on diagnosis/procedure codes
        Index(
            "ix_claims_diagnosis_codes_gin",
            "diagnosis_codes",
            postgresql_using="gin",
        ),
        Index(
            "ix_claims_procedure_codes_gin",
            "procedure_codes",
            postgresql_using="gin",
        ),
    )

    def __repr__(self) -> str:
        return f"<Claim id={self.id} number={self.claim_number} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# Applications (Underwriting)
# ─────────────────────────────────────────────────────────────────────────────

class Application(TimestampMixin, Base):
    """
    Insurance application submitted by a prospective member.
    Drives the AI underwriting workflow to produce a risk decision.
    """
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    application_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
    )
    applicant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    policy_type: Mapped[PolicyType] = mapped_column(
        Enum(PolicyType, name="policy_type", create_type=True),
        nullable=False,
    )
    requested_coverage_limit: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False,
    )

    # Health questionnaire — free-form JSONB (questions + answers)
    health_questionnaire: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True,
        comment="Structured AoB / health declaration questions and responses",
    )

    # ── AI Underwriting Outputs ────────────────────────────────────────────
    underwriting_score: Mapped[float | None] = mapped_column(
        Numeric(6, 4), nullable=True,
        comment="Composite AI risk score [0.0–100.0]",
    )
    risk_tier: Mapped[RiskTier | None] = mapped_column(
        Enum(RiskTier, name="risk_tier", create_type=True),
        nullable=True,
    )
    ai_underwriting_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    suggested_premium: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # ── Workflow State ─────────────────────────────────────────────────────
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus, name="application_status", create_type=True),
        nullable=False,
        default=ApplicationStatus.DRAFT,
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Underwriter who made the final decision",
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Relationships ──────────────────────────────────────────────────────
    applicant: Mapped["User"] = relationship(
        "User",
        back_populates="applications",
        foreign_keys=[applicant_id],
        lazy="selectin",
    )
    reviewer: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[reviewed_by],
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_applications_applicant_status", "applicant_id", "status"),
        Index("ix_applications_risk_tier", "risk_tier"),
        Index(
            "ix_applications_health_questionnaire_gin",
            "health_questionnaire",
            postgresql_using="gin",
        ),
    )

    def __repr__(self) -> str:
        return f"<Application id={self.id} number={self.application_number} status={self.status}>"
