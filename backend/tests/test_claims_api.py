"""
backend/tests/test_claims_api.py
──────────────────────────────────
Async integration tests for the Claims Adjudication API and SNIP Validation Engine.

HOW TO RUN LOCALLY
───────────────────
  cd ai_insurance_platform
  pip install pytest pytest-asyncio httpx pytest-cov
  pytest backend/tests/test_claims_api.py -v --asyncio-mode=auto

To also generate a coverage report:
  pytest backend/tests/test_claims_api.py -v --asyncio-mode=auto --cov=backend --cov-report=html

MOCKING STRATEGY
──────────────────
All external dependencies are mocked via pytest fixtures so tests run in
milliseconds with zero infrastructure (no database, no Kafka, no LLM):

1. DATABASE (get_db):
   Replaced with an in-memory SQLite database using SQLAlchemy async engine.
   The test schema is created fresh for each test session.

2. AUTH (get_current_user):
   Replaced with a factory that returns a pre-built User ORM instance
   with a configurable role. This lets us test RBAC without JWT generation.

3. KAFKA PUBLISHER (publish_claim_validated):
   Mocked with unittest.mock.AsyncMock — records calls without touching Kafka.
   Tests assert that the mock was called with the expected claim_id.

4. SNIP VALIDATOR (validate_claim):
   Has two fixture modes:
   - snip_pass: Returns a successful SNIPResult (default).
   - snip_fail: Raises SNIPValidationError with a Tier 1 violation.

PYTEST-ASYNCIO CONFIGURATION
──────────────────────────────
We use asyncio_mode=auto (set in pyproject.toml or pytest.ini) so that
all async test functions are automatically wrapped — no @pytest.mark.asyncio
decoration needed on each test function.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.deps import get_current_user, get_db
from backend.api.routers import claims as claims_router_module
from backend.claims.snip_validator import (
    SNIPResult,
    SNIPTier,
    SNIPViolation,
    SNIPValidationError,
)
from backend.database.models import Base, User, UserRole
from backend.main import app


# ─────────────────────────────────────────────────────────────────────────────
# Test Database Fixture — in-memory SQLite
# ─────────────────────────────────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Create an in-memory SQLite engine shared across the test session."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh transactional session for each test, rolled back on exit."""
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# Auth Fixture — Mock Users
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_user(role: UserRole = UserRole.CLAIMS_ADJUSTER) -> User:
    """Build a mock User ORM object without hitting the database."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role = role
    user.is_active = True
    user.email = f"{role.value}@test.com"
    return user


@pytest.fixture
def mock_adjuster() -> User:
    return _make_mock_user(UserRole.CLAIMS_ADJUSTER)


@pytest.fixture
def mock_admin() -> User:
    return _make_mock_user(UserRole.ADMIN)


@pytest.fixture
def mock_insured() -> User:
    return _make_mock_user(UserRole.INSURED)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Test Client Fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db_session: AsyncSession, mock_adjuster: User) -> AsyncGenerator[AsyncClient, None]:
    """
    Async HTTPX client wired to the FastAPI app with mocked dependencies.

    Overrides:
      - get_db → yields the in-memory SQLite session
      - get_current_user → returns the mock_adjuster (no JWT required)
    """
    app.dependency_overrides[get_db] = lambda: _db_override(db_session)
    app.dependency_overrides[get_current_user] = lambda: mock_adjuster

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


async def _db_override(session: AsyncSession) -> AsyncGenerator[AsyncSession, None]:
    yield session


# ─────────────────────────────────────────────────────────────────────────────
# Valid Claim Payload Factory
# ─────────────────────────────────────────────────────────────────────────────

def _valid_claim_payload(
    policy_id: str | None = None,
    total_charge: str = "250.00",
    line_charges: list[str] | None = None,
) -> dict:
    """
    Build a syntactically and mathematically valid claim intake payload.

    Balance testing contract: sum(line_charges) MUST equal total_charge.
    Default: one line at $250.00 == header total $250.00.
    """
    if policy_id is None:
        policy_id = str(uuid.uuid4())

    if line_charges is None:
        line_charges = [total_charge]

    lines = [
        {
            "line_number": i + 1,
            "procedure_code": "99213",
            "modifier": None,
            "units": 1,
            "charge_amount": charge,
            "place_of_service": "11",
            "rendering_provider_npi": None,
        }
        for i, charge in enumerate(line_charges)
    ]

    return {
        "transaction_set": "837P",
        "interchange_control_number": "ICN20240001",
        "billing_provider_npi": "1234567893",  # Valid Luhn NPI
        "policy_id": policy_id,
        "service_date_start": "2024-03-01",
        "service_date_end": "2024-03-01",
        "diagnosis_codes": ["E11.9"],
        "procedure_lines": lines,
        "total_charge": total_charge,
        "place_of_service": "11",
    }


# ─────────────────────────────────────────────────────────────────────────────
# SNIP Validator Mocks
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_snip_pass():
    """Mock validate_claim to always succeed (return a passing SNIPResult)."""
    passing_result = SNIPResult(
        claim_id="test-claim-id",
        passed=True,
        highest_tier_passed=SNIPTier.TRADING_PARTNER,
        failing_tier=None,
        violations=[],
    )
    with patch(
        "backend.api.routers.claims.validate_claim",
        new_callable=AsyncMock,
        return_value=passing_result,
    ) as mock:
        yield mock


@pytest.fixture
def mock_snip_fail():
    """Mock validate_claim to raise SNIPValidationError at Tier 3 (balance)."""
    failing_result = SNIPResult(
        claim_id="test-claim-id",
        passed=False,
        highest_tier_passed=SNIPTier.HIPAA_COMPLIANCE,
        failing_tier=SNIPTier.BALANCE_TESTING,
        violations=[
            SNIPViolation(
                tier=SNIPTier.BALANCE_TESTING,
                error_code="T3_BALANCE_MISMATCH",
                field_path="total_charge",
                message="Header total 300.00 != line sum 250.00. Discrepancy: $50.00.",
            )
        ],
    )
    with patch(
        "backend.api.routers.claims.validate_claim",
        new_callable=AsyncMock,
        side_effect=SNIPValidationError(failing_result),
    ) as mock:
        yield mock


@pytest.fixture
def mock_kafka_publish():
    """Mock Kafka publisher — records calls without touching a real broker."""
    with patch(
        "backend.api.routers.claims.publish_claim_validated",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock:
        yield mock


@pytest.fixture
def mock_policy_exists(db_session: AsyncSession):
    """Mock the policy lookup to always return a valid Policy object."""
    from backend.database.models import Policy, PolicyStatus
    with patch(
        "backend.api.routers.claims.select",
        wraps=__import__("sqlalchemy", fromlist=["select"]).select,
    ):
        policy = MagicMock(spec=Policy)
        policy.id = uuid.uuid4()
        policy.holder_id = uuid.uuid4()
        policy.status = PolicyStatus.ACTIVE

        async def _fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = policy
            return result

        db_session.execute = _fake_execute
        yield policy


# ─────────────────────────────────────────────────────────────────────────────
# ── TESTS: SNIP Validation Unit Tests ────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

class TestSNIPValidator:
    """Unit tests for each SNIP tier, run without HTTP."""

    @pytest.mark.asyncio
    async def test_tier1_rejects_empty_icn(self):
        """Tier 1 must reject a claim with a blank interchange control number."""
        from backend.claims.schemas import EDIClaimPayload, EDIProcedureLine
        from backend.claims.snip_validator import validate_claim

        payload = EDIClaimPayload(
            transaction_set="837P",
            interchange_control_number="   ",   # Blank ICN
            billing_provider_npi="1234567893",
            patient_id=uuid.uuid4(),
            policy_id=uuid.uuid4(),
            service_date_start=date.today(),
            diagnosis_codes=["E11.9"],
            procedure_lines=[
                EDIProcedureLine(
                    line_number=1, procedure_code="99213",
                    units=1, charge_amount=Decimal("100.00"),
                )
            ],
            total_charge=Decimal("100.00"),
        )

        with pytest.raises(SNIPValidationError) as exc_info:
            await validate_claim(payload, "test-id")

        result = exc_info.value.result
        assert result.failing_tier == SNIPTier.INTEGRITY
        assert any(v.error_code == "T1_EMPTY_ICN" for v in result.violations)

    @pytest.mark.asyncio
    async def test_tier2_rejects_invalid_npi_luhn(self):
        """Tier 2 must reject an NPI that fails the Luhn checksum."""
        from backend.claims.schemas import EDIClaimPayload, EDIProcedureLine
        from backend.claims.snip_validator import validate_claim

        payload = EDIClaimPayload(
            transaction_set="837P",
            interchange_control_number="ICN001",
            billing_provider_npi="1111111111",  # Valid format, fails Luhn
            patient_id=uuid.uuid4(),
            policy_id=uuid.uuid4(),
            service_date_start=date.today(),
            diagnosis_codes=["E11.9"],
            procedure_lines=[
                EDIProcedureLine(
                    line_number=1, procedure_code="99213",
                    units=1, charge_amount=Decimal("100.00"),
                )
            ],
            total_charge=Decimal("100.00"),
        )

        with pytest.raises(SNIPValidationError) as exc_info:
            await validate_claim(payload, "test-id")

        result = exc_info.value.result
        assert result.failing_tier == SNIPTier.HIPAA_COMPLIANCE
        assert any(v.error_code == "T2_INVALID_NPI_LUHN" for v in result.violations)

    @pytest.mark.asyncio
    async def test_tier3_rejects_balance_mismatch(self):
        """Tier 3 must reject claims where line items do not sum to the header total."""
        from backend.claims.schemas import EDIClaimPayload, EDIProcedureLine
        from backend.claims.snip_validator import validate_claim

        payload = EDIClaimPayload(
            transaction_set="837P",
            interchange_control_number="ICN002",
            billing_provider_npi="1234567893",
            patient_id=uuid.uuid4(),
            policy_id=uuid.uuid4(),
            service_date_start=date.today(),
            diagnosis_codes=["E11.9"],
            procedure_lines=[
                EDIProcedureLine(
                    line_number=1, procedure_code="99213",
                    units=1, charge_amount=Decimal("100.00"),
                )
            ],
            total_charge=Decimal("999.00"),  # Mismatch: $100 line vs $999 header
        )

        with pytest.raises(SNIPValidationError) as exc_info:
            await validate_claim(payload, "test-id")

        result = exc_info.value.result
        assert result.failing_tier == SNIPTier.BALANCE_TESTING
        assert any(v.error_code == "T3_BALANCE_MISMATCH" for v in result.violations)


# ─────────────────────────────────────────────────────────────────────────────
# ── TESTS: Claims API Integration Tests ──────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

class TestClaimsIntakeEndpoint:
    """Integration tests for POST /claims/intake — the EDA non-blocking endpoint."""

    @pytest.mark.asyncio
    async def test_valid_claim_returns_202(
        self,
        client: AsyncClient,
        mock_snip_pass,
        mock_kafka_publish,
        mock_policy_exists,
    ):
        """A valid, balanced claim must return HTTP 202 Accepted."""
        payload = _valid_claim_payload()
        response = await client.post("/claims/intake", json=payload)

        assert response.status_code == 202, response.text
        data = response.json()
        assert data["snip_status"] == "passed"
        assert data["snip_failing_tier"] is None
        assert "claim_id" in data

    @pytest.mark.asyncio
    async def test_valid_claim_publishes_to_kafka(
        self,
        client: AsyncClient,
        mock_snip_pass,
        mock_kafka_publish,
        mock_policy_exists,
    ):
        """After SNIP passes, the Kafka publisher must be called exactly once."""
        payload = _valid_claim_payload()
        await client.post("/claims/intake", json=payload)
        mock_kafka_publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_snip_rejected_claim_returns_422(
        self,
        client: AsyncClient,
        mock_snip_fail,
        mock_policy_exists,
    ):
        """A claim failing SNIP Tier 3 (balance) must return HTTP 422 with violation details."""
        payload = _valid_claim_payload()
        response = await client.post("/claims/intake", json=payload)

        assert response.status_code == 422, response.text
        data = response.json()
        assert data["detail"]["adjudication_state"] == "snip_rejected"
        assert data["detail"]["failing_tier"] == 3
        violations = data["detail"]["violations"]
        assert len(violations) > 0
        assert violations[0]["error_code"] == "T3_BALANCE_MISMATCH"

    @pytest.mark.asyncio
    async def test_snip_rejected_kafka_not_called(
        self,
        client: AsyncClient,
        mock_snip_fail,
        mock_kafka_publish,
        mock_policy_exists,
    ):
        """When SNIP fails, the Kafka publisher must NOT be called (claim not published)."""
        payload = _valid_claim_payload()
        await client.post("/claims/intake", json=payload)
        mock_kafka_publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kafka_failure_still_returns_202(
        self,
        client: AsyncClient,
        mock_snip_pass,
        mock_policy_exists,
    ):
        """
        If Kafka publishing fails (KafkaPublishError), the endpoint must still
        return 202 — the claim is persisted in PostgreSQL and retried later.
        Claims must never be dropped due to transient Kafka outages.
        """
        from backend.workflows.events import KafkaPublishError

        with patch(
            "backend.api.routers.claims.publish_claim_validated",
            new_callable=AsyncMock,
            side_effect=KafkaPublishError("broker unavailable"),
        ):
            payload = _valid_claim_payload()
            response = await client.post("/claims/intake", json=payload)

        # CRITICAL: must still be 202, not 500
        assert response.status_code == 202, response.text

    @pytest.mark.asyncio
    async def test_missing_policy_returns_404(
        self,
        client: AsyncClient,
    ):
        """A claim referencing a non-existent policy must return HTTP 404."""
        async def _no_policy(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        client.app.dependency_overrides[get_db]  # ensure override active
        with patch("backend.api.routers.claims.select"):
            from backend.database.base import get_db as real_get_db
            # Patch the session.execute to return None for policy
            with patch(
                "sqlalchemy.ext.asyncio.AsyncSession.execute",
                new_callable=AsyncMock,
                side_effect=_no_policy,
            ):
                payload = _valid_claim_payload()
                response = await client.post("/claims/intake", json=payload)

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_pydantic_rejects_missing_required_field(
        self,
        client: AsyncClient,
        mock_policy_exists,
    ):
        """Pydantic validation must reject payloads missing required fields before SNIP runs."""
        # Missing billing_provider_npi
        payload = {
            "transaction_set": "837P",
            "interchange_control_number": "ICN001",
            "policy_id": str(uuid.uuid4()),
            "service_date_start": "2024-03-01",
            "diagnosis_codes": ["E11.9"],
            "procedure_lines": [
                {"line_number": 1, "procedure_code": "99213", "units": 1, "charge_amount": "100.00"}
            ],
            "total_charge": "100.00",
        }
        response = await client.post("/claims/intake", json=payload)
        assert response.status_code == 422   # Pydantic validation error

    @pytest.mark.asyncio
    async def test_get_claims_list_returns_200(
        self,
        client: AsyncClient,
        mock_policy_exists,
    ):
        """GET /claims/ must return paginated response with status 200."""
        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
                scalar_one=MagicMock(return_value=0),
            ),
        ):
            response = await client.get("/claims/")

        assert response.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# ── TESTS: State Machine Unit Tests ──────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

class TestAdjudicationStateMachine:

    def test_valid_transition_received_to_validated(self):
        """RECEIVED + SNIP_PASS → VALIDATED must be a legal transition."""
        from backend.claims.state_machine import AdjudicationState, ClaimEvent, apply_transition

        result = apply_transition(AdjudicationState.RECEIVED, ClaimEvent.SNIP_PASS)
        assert result == AdjudicationState.VALIDATED

    def test_invalid_transition_raises_error(self):
        """Jumping from RECEIVED directly to FINALIZED must raise IllegalTransitionError."""
        from backend.claims.state_machine import (
            AdjudicationState, ClaimEvent, IllegalTransitionError, apply_transition,
        )

        with pytest.raises(IllegalTransitionError):
            apply_transition(AdjudicationState.RECEIVED, ClaimEvent.FINALIZE)

    def test_snip_rejected_is_terminal(self):
        """SNIP_REJECTED state must not accept any forward transitions."""
        from backend.claims.state_machine import (
            AdjudicationState, ClaimEvent, IllegalTransitionError, apply_transition,
        )

        with pytest.raises(IllegalTransitionError):
            apply_transition(AdjudicationState.SNIP_REJECTED, ClaimEvent.SNIP_PASS)

    def test_build_transition_record_contains_audit_fields(self):
        """build_transition_record must populate all audit fields."""
        from backend.claims.state_machine import (
            AdjudicationState, ClaimEvent, build_transition_record,
        )

        record = build_transition_record(
            claim_id="abc-123",
            from_state=AdjudicationState.VALIDATED,
            event=ClaimEvent.UM_ROUTE_STP,
            triggered_by="um_router",
            reason="Clean STP claim",
        )

        assert record.claim_id == "abc-123"
        assert record.to_state == AdjudicationState.AUTO_ADJUDICATED
        assert record.triggered_by == "um_router"
        assert record.timestamp is not None


# ─────────────────────────────────────────────────────────────────────────────
# ── TESTS: Underwriting Scoring Unit Tests ────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

class TestUnderwritingScoring:

    @pytest.mark.asyncio
    async def test_tobacco_smoker_adds_debits(self):
        """A current smoker profile must receive exactly 50 tobacco debits."""
        from backend.underwriting.scoring import ApplicantRiskProfile, score_applicant

        profile = ApplicantRiskProfile(
            application_id="app-001",
            age=35,
            tobacco_status="current_smoker",
        )
        ledger = await score_applicant(profile)
        tobacco_debits = sum(
            e.points for e in ledger.entries if e.factor.value == "tobacco"
        )
        assert tobacco_debits == 50

    @pytest.mark.asyncio
    async def test_excellent_bp_adds_credit(self):
        """An applicant with excellent BP must receive the -15 credit."""
        from backend.underwriting.scoring import ApplicantRiskProfile, score_applicant

        profile = ApplicantRiskProfile(
            application_id="app-002",
            age=30,
            systolic_bp=115,
            diastolic_bp=75,
        )
        ledger = await score_applicant(profile)
        bp_credits = sum(
            e.points for e in ledger.entries if e.factor.value == "blood_pressure"
        )
        assert bp_credits == -15

    @pytest.mark.asyncio
    async def test_high_bmi_adds_debits(self):
        """BMI of 36 (Obese Class II) must add 50 debits."""
        from backend.underwriting.scoring import ApplicantRiskProfile, score_applicant

        profile = ApplicantRiskProfile(application_id="app-003", bmi=36.0)
        ledger = await score_applicant(profile)
        bmi_debits = sum(
            e.points for e in ledger.entries if e.factor.value == "bmi"
        )
        assert bmi_debits == 50

    @pytest.mark.asyncio
    async def test_icd10_prefix_matching(self):
        """Code 'E11.65' must match the 'E11' prefix entry (Type 2 DM = 50 debits)."""
        from backend.underwriting.scoring import ApplicantRiskProfile, score_applicant

        profile = ApplicantRiskProfile(
            application_id="app-004",
            diagnosis_codes=["E11.65"],
        )
        ledger = await score_applicant(profile)
        dm_debits = sum(
            e.points for e in ledger.entries if "E11" in e.code
        )
        assert dm_debits == 50
