"""
seed.py — Populate the database with realistic test data for development.
Run from project root:
  DATABASE_URL=postgresql+asyncpg://insurance_user:insurance_pass@localhost:5433/insurance_db \
  CHROMA_HOST=localhost CHROMA_PORT=8001 \
  .venv/bin/python seed.py
"""
import asyncio, uuid, os
from datetime import datetime, date, timedelta
from decimal import Decimal

DATABASE_URL = "postgresql+asyncpg://insurance_user:insurance_pass@localhost:5433/insurance_db"
os.environ.setdefault("DATABASE_URL", DATABASE_URL)
os.environ.setdefault("CHROMA_HOST", "localhost")
os.environ.setdefault("CHROMA_PORT", "8001")

import bcrypt as _bcrypt
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

def _hash(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=10)).decode()


async def seed():
    from backend.database.models import (
        User, UserRole,
        Application, ApplicationStatus, PolicyType as AppPolicyType, RiskTier,
        Claim, ClaimStatus,
        Policy, PolicyStatus, PolicyType,
    )

    async with AsyncSessionLocal() as db:
        # ── Users ────────────────────────────────────────────────────────────
        admin_id   = uuid.uuid4()
        uw_id      = uuid.uuid4()
        insured1   = uuid.uuid4()
        insured2   = uuid.uuid4()

        users = [
            User(
                id=admin_id, tenant_id="medintel", email="admin@medintel.ai",
                hashed_password=_hash("Admin1234!"),
                full_name="Medical Director", role=UserRole.ADMIN,
                is_active=True, is_verified=True,
            ),
            User(
                id=uw_id, tenant_id="medintel", email="uw@medintel.ai",
                hashed_password=_hash("Underwriter1!"),
                full_name="Senior Underwriter", role=UserRole.UNDERWRITER,
                is_active=True, is_verified=True,
            ),
            User(
                id=insured1, tenant_id="medintel", email="eleanor@example.com",
                hashed_password=_hash("Patient1234!"),
                full_name="Eleanor Vance", role=UserRole.INSURED,
                is_active=True, is_verified=True,
            ),
            User(
                id=insured2, tenant_id="medintel", email="marcus@example.com",
                hashed_password=_hash("Patient1234!"),
                full_name="Marcus Thorne", role=UserRole.INSURED,
                is_active=True, is_verified=True,
            ),
        ]
        db.add_all(users)
        await db.flush()
        print(f"  ✓ {len(users)} users")

        # ── Policies ─────────────────────────────────────────────────────────
        p1_id = uuid.uuid4()
        p2_id = uuid.uuid4()
        policies = [
            Policy(
                id=p1_id, tenant_id="medintel", policy_number="POL-2023-001",
                holder_id=insured1, policy_type=PolicyType.INDIVIDUAL,
                premium_amount=Decimal("850.00"),
                coverage_limit=Decimal("500000"),
                deductible=Decimal("1500.00"),
                effective_date=date(2023, 1, 1),
                expiry_date=date(2024, 12, 31),
                status=PolicyStatus.ACTIVE,
            ),
            Policy(
                id=p2_id, tenant_id="medintel", policy_number="POL-2023-002",
                holder_id=insured2, policy_type=PolicyType.INDIVIDUAL,
                premium_amount=Decimal("620.00"),
                coverage_limit=Decimal("250000"),
                deductible=Decimal("2000.00"),
                effective_date=date(2023, 6, 1),
                expiry_date=date(2024, 12, 31),
                status=PolicyStatus.ACTIVE,
            ),
        ]
        db.add_all(policies)
        await db.flush()
        print(f"  ✓ {len(policies)} policies")

        # ── Applications ─────────────────────────────────────────────────────
        apps = [
            Application(
                id=uuid.uuid4(), tenant_id="medintel", application_number="APP-9024-X",
                applicant_id=insured1, policy_type=AppPolicyType.INDIVIDUAL,
                requested_coverage_limit=Decimal("500000"),
                status=ApplicationStatus.UNDER_REVIEW,
                underwriting_score=85, risk_tier=RiskTier.SUBSTANDARD,
                ai_underwriting_notes=(
                    "Elevated risk due to chronic respiratory issues and newly identified "
                    "metabolic markers. Manual review required."
                ),
            ),
            Application(
                id=uuid.uuid4(), tenant_id="medintel", application_number="APP-8831-M",
                applicant_id=insured2, policy_type=AppPolicyType.INDIVIDUAL,
                requested_coverage_limit=Decimal("250000"),
                status=ApplicationStatus.APPROVED,
                underwriting_score=22, risk_tier=RiskTier.PREFERRED,
                ai_underwriting_notes=(
                    "Low-risk profile. Recommended for standard table with preferred rates."
                ),
            ),
            Application(
                id=uuid.uuid4(), tenant_id="medintel", application_number="APP-7712-S",
                applicant_id=insured1, policy_type=AppPolicyType.GROUP,
                requested_coverage_limit=Decimal("100000"),
                status=ApplicationStatus.SUBMITTED,
                underwriting_score=None, risk_tier=None,
                ai_underwriting_notes=None,
            ),
            Application(
                id=uuid.uuid4(), tenant_id="medintel", application_number="APP-6601-D",
                applicant_id=insured2, policy_type=AppPolicyType.INDIVIDUAL,
                requested_coverage_limit=Decimal("1000000"),
                status=ApplicationStatus.UNDER_REVIEW,
                underwriting_score=58, risk_tier=RiskTier.STANDARD,
                ai_underwriting_notes=(
                    "Moderate risk. Type 2 Diabetes Mellitus under control. "
                    "Standard table rating suggested."
                ),
            ),
        ]
        db.add_all(apps)
        await db.flush()
        print(f"  ✓ {len(apps)} applications")

        # ── Claims ───────────────────────────────────────────────────────────
        claims = [
            Claim(
                id=uuid.uuid4(), tenant_id="medintel", claim_number="CLM-2023-8901",
                policy_id=p1_id, claimant_id=insured1,
                status=ClaimStatus.IN_REVIEW,
                billed_amount=Decimal("1245.00"),
                allowed_amount=Decimal("1100.00"),
                paid_amount=None,
                service_date_start=date(2023, 10, 15),
                service_date_end=date(2023, 10, 15),
                diagnosis_codes=["I10", "E78.5"],
                fraud_score=0.12,
                ai_notes="Hypertension follow-up with lipid panel. Standard preventive care.",
            ),
            Claim(
                id=uuid.uuid4(), tenant_id="medintel", claim_number="CLM-2023-8902",
                policy_id=p2_id, claimant_id=insured2,
                status=ClaimStatus.SUBMITTED,
                billed_amount=Decimal("890.50"),
                allowed_amount=None, paid_amount=None,
                service_date_start=date(2023, 10, 18),
                service_date_end=date(2023, 10, 18),
                diagnosis_codes=["E11.9"],
                fraud_score=0.08,
                ai_notes="Type 2 Diabetes Mellitus management visit with HbA1c lab.",
            ),
            Claim(
                id=uuid.uuid4(), tenant_id="medintel", claim_number="CLM-2023-8903",
                policy_id=p1_id, claimant_id=insured1,
                status=ClaimStatus.APPROVED,
                billed_amount=Decimal("320.00"),
                allowed_amount=Decimal("295.00"),
                paid_amount=Decimal("295.00"),
                service_date_start=date(2023, 10, 20),
                service_date_end=date(2023, 10, 20),
                diagnosis_codes=["J20.9"],
                fraud_score=0.03,
                ai_notes="Acute bronchitis treatment. Approved.",
            ),
            Claim(
                id=uuid.uuid4(), tenant_id="medintel", claim_number="CLM-2023-8904",
                policy_id=p2_id, claimant_id=insured2,
                status=ClaimStatus.DENIED,
                billed_amount=Decimal("0.00"),
                allowed_amount=Decimal("0.00"),
                paid_amount=Decimal("0.00"),
                service_date_start=date(2023, 10, 22),
                service_date_end=date(2023, 10, 22),
                diagnosis_codes=[],
                fraud_score=None,
                ai_notes="Unreadable document format. Could not extract data.",
            ),
        ]
        db.add_all(claims)
        await db.commit()
        print(f"  ✓ {len(claims)} claims")

        print("\n✅ Seeding complete!")
        print("   Admin:       admin@medintel.ai  / Admin1234!")
        print("   Underwriter: uw@medintel.ai     / Underwriter1!")


asyncio.run(seed())
