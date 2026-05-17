"""
backend/tests/test_claims_api.py — extended
Adds tests for:
  • POST /claims/upload  (multipart form-data)
  • PATCH /claims/{id}/status

Run with existing test suite:
  pytest backend/tests/test_claims_api.py -v --asyncio-mode=auto
"""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.deps import get_current_user, get_db
from backend.claims.snip_validator import (
    SNIPResult,
    SNIPTier,
    SNIPViolation,
    SNIPValidationError,
)
from backend.database.models import Base, User, UserRole
from backend.main import app

# ─── Shared fixtures (identical to original conftest pattern) ────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


def _make_mock_user(role: UserRole = UserRole.CLAIMS_ADJUSTER) -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
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


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, mock_adjuster: User) -> AsyncGenerator[AsyncClient, None]:
    async def _db_gen():
        yield db_session

    app.dependency_overrides[get_db] = _db_gen
    app.dependency_overrides[get_current_user] = lambda: mock_adjuster

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def mock_snip_pass():
    result = SNIPResult(
        claim_id="test",
        passed=True,
        highest_tier_passed=SNIPTier.TRADING_PARTNER,
        failing_tier=None,
        violations=[],
    )
    with patch("backend.api.routers.claims.validate_claim", new_callable=AsyncMock, return_value=result) as m:
        yield m


@pytest.fixture
def mock_kafka_publish():
    with patch("backend.api.routers.claims.publish_claim_validated", new_callable=AsyncMock, return_value=None) as m:
        yield m


@pytest.fixture
def mock_policy_row():
    """Return a mock Policy ORM row via db.execute mock."""
    from backend.database.models import Policy, PolicyStatus

    policy = MagicMock(spec=Policy)
    policy.id = uuid.uuid4()
    policy.holder_id = uuid.uuid4()
    policy.status = PolicyStatus.ACTIVE
    policy.tenant_id = uuid.uuid4()
    return policy


# ─── Task 12a: POST /claims/upload ───────────────────────────────────────────

class TestClaimsUploadEndpoint:
    """Integration tests for POST /claims/upload (multipart/form-data)."""

    @pytest.mark.asyncio
    async def test_upload_pdf_returns_202(
        self,
        client: AsyncClient,
        mock_snip_pass,
        mock_kafka_publish,
        mock_policy_row,
    ):
        """A valid PDF upload must trigger mock EDI extraction and return 202."""
        fake_pdf = io.BytesIO(b"%PDF-1.4 fake content for testing")
        fake_pdf.name = "test_claim.pdf"

        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(mock_policy_row),
        ):
            response = await client.post(
                "/claims/upload",
                files={"file": ("test_claim.pdf", fake_pdf, "application/pdf")},
            )

        assert response.status_code == 202, response.text
        data = response.json()
        assert "claim_id" in data
        assert data["snip_status"] == "passed"

    @pytest.mark.asyncio
    async def test_upload_png_returns_202(
        self,
        client: AsyncClient,
        mock_snip_pass,
        mock_kafka_publish,
        mock_policy_row,
    ):
        """PNG image uploads must also route through the EDA pipeline."""
        fake_png = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(mock_policy_row),
        ):
            response = await client.post(
                "/claims/upload",
                files={"file": ("scan.png", fake_png, "image/png")},
            )

        assert response.status_code == 202, response.text

    @pytest.mark.asyncio
    async def test_upload_no_policy_returns_404(
        self,
        client: AsyncClient,
    ):
        """Upload with no policy on the account must return 404."""
        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(None),
        ):
            fake_pdf = io.BytesIO(b"fake")
            response = await client.post(
                "/claims/upload",
                files={"file": ("x.pdf", fake_pdf, "application/pdf")},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_upload_mock_claim_has_deterministic_icn(
        self,
        client: AsyncClient,
        mock_snip_pass,
        mock_kafka_publish,
        mock_policy_row,
    ):
        """
        Two uploads of the same filename must produce the same ICN
        (deterministic hash — supports idempotency).
        """
        fake_pdf = io.BytesIO(b"identical content")

        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(mock_policy_row),
        ):
            r1 = await client.post("/claims/upload", files={"file": ("same.pdf", io.BytesIO(b"identical content"), "application/pdf")})
            r2 = await client.post("/claims/upload", files={"file": ("same.pdf", io.BytesIO(b"identical content"), "application/pdf")})

        assert r1.status_code == 202
        assert r2.status_code == 202
        # Both should share the same claim_number (deterministic ICN → same claim_number prefix)
        assert r1.json()["claim_number"] == r2.json()["claim_number"]

    @pytest.mark.asyncio
    async def test_upload_publishes_to_kafka(
        self,
        client: AsyncClient,
        mock_snip_pass,
        mock_kafka_publish,
        mock_policy_row,
    ):
        """Successful upload must call the Kafka publisher exactly once."""
        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(mock_policy_row),
        ):
            await client.post(
                "/claims/upload",
                files={"file": ("x.pdf", io.BytesIO(b"data"), "application/pdf")},
            )

        mock_kafka_publish.assert_awaited_once()


# ─── Task 12b: PATCH /claims/{id}/status ─────────────────────────────────────

class TestClaimsStatusPatchEndpoint:
    """Integration tests for PATCH /claims/{claim_id}/status."""

    def _make_claim_row(self, tenant_id=None):
        from backend.database.models import Claim, ClaimStatus

        claim = MagicMock(spec=Claim)
        claim.id = uuid.uuid4()
        claim.tenant_id = tenant_id or uuid.uuid4()
        claim.status = ClaimStatus.IN_REVIEW
        claim.denial_reason = None
        claim.adjudicated_by = None
        claim.adjudicated_at = None
        claim.claim_number = "CLM-TEST-001"
        claim.billed_amount = Decimal("250.00")
        claim.allowed_amount = None
        claim.ai_notes = None
        claim.fraud_score = None
        claim.created_at = datetime.now(timezone.utc)
        return claim

    @pytest.mark.asyncio
    async def test_approve_claim_returns_200(self, client: AsyncClient, mock_adjuster: User):
        """Approving a claim must return HTTP 200 with updated status."""
        claim = self._make_claim_row(tenant_id=mock_adjuster.tenant_id)
        claim_id = claim.id

        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(claim),
        ):
            with patch("sqlalchemy.ext.asyncio.AsyncSession.flush", new_callable=AsyncMock):
                response = await client.patch(
                    f"/claims/{claim_id}/status",
                    json={"status": "approved"},
                )

        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_deny_claim_with_reason(self, client: AsyncClient, mock_adjuster: User):
        """Denying a claim with a denial_reason must persist the reason."""
        claim = self._make_claim_row(tenant_id=mock_adjuster.tenant_id)
        claim_id = claim.id

        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(claim),
        ):
            with patch("sqlalchemy.ext.asyncio.AsyncSession.flush", new_callable=AsyncMock):
                response = await client.patch(
                    f"/claims/{claim_id}/status",
                    json={"status": "denied", "denial_reason": "Not medically necessary per policy section 4.2"},
                )

        assert response.status_code == 200, response.text
        # The endpoint should have set claim.denial_reason
        assert claim.denial_reason == "Not medically necessary per policy section 4.2"

    @pytest.mark.asyncio
    async def test_patch_nonexistent_claim_returns_404(self, client: AsyncClient):
        """Patching a claim that doesn't exist must return 404."""
        random_id = uuid.uuid4()

        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(None),
        ):
            response = await client.patch(
                f"/claims/{random_id}/status",
                json={"status": "approved"},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_insured_cannot_patch_status(self, db_session: AsyncSession):
        """INSURED role must be forbidden from the PATCH status endpoint (RBAC)."""
        mock_insured = _make_mock_user(UserRole.INSURED)

        app.dependency_overrides[get_db] = lambda: _db_override_gen(db_session)
        app.dependency_overrides[get_current_user] = lambda: mock_insured

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            response = await ac.patch(
                f"/claims/{uuid.uuid4()}/status",
                json={"status": "approved"},
            )

        app.dependency_overrides.clear()
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_status_value_rejected(self, client: AsyncClient):
        """Sending an unrecognised status string must return HTTP 422."""
        response = await client.patch(
            f"/claims/{uuid.uuid4()}/status",
            json={"status": "MADE_UP_STATUS"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_adjudicated_by_is_set(self, client: AsyncClient, mock_adjuster: User):
        """The adjudicated_by field must be set to the current user's ID."""
        claim = self._make_claim_row(tenant_id=mock_adjuster.tenant_id)
        claim_id = claim.id

        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _make_execute_result(claim),
        ):
            with patch("sqlalchemy.ext.asyncio.AsyncSession.flush", new_callable=AsyncMock):
                await client.patch(f"/claims/{claim_id}/status", json={"status": "approved"})

        assert claim.adjudicated_by == mock_adjuster.id
        assert claim.adjudicated_at is not None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_execute_result(obj):
    result = MagicMock()
    result.scalar_one_or_none.return_value = obj
    result.scalars.return_value.all.return_value = [obj] if obj else []
    result.scalar_one.return_value = 0
    return result


async def _db_override_gen(session: AsyncSession):
    yield session
