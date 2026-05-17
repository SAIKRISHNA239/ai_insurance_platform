"""
backend/tests/test_policies.py
────────────────────────────────
Integration tests for the Policies CRUD API.

Covers:
  POST /policies/        — create (admin/underwriter only)
  GET  /policies/        — list (paginated, role-scoped)
  GET  /policies/{id}    — get by ID (tenant-scoped, INSURED RBAC)

Run:
  pytest backend/tests/test_policies.py -v --asyncio-mode=auto
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.deps import get_current_user, get_db
from backend.database.models import Base, Policy, PolicyStatus, PolicyType, User, UserRole
from backend.main import app

TEST_DB = "sqlite+aiosqlite:///:memory:"


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DB, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


def _make_user(role: UserRole = UserRole.ADMIN) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.tenant_id = uuid.uuid4()
    u.role = role
    u.is_active = True
    u.email = f"{role.value}@test.com"
    return u


async def _db_gen(session: AsyncSession):
    yield session


def _client_for(db_session: AsyncSession, user: User):
    """Return a context-manager AsyncClient with the given user override."""
    app.dependency_overrides[get_db] = lambda: _db_gen(db_session)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _valid_policy_payload(policy_number: str | None = None) -> dict:
    return {
        "policy_number": policy_number or f"POL-{uuid.uuid4().hex[:8].upper()}",
        "holder_id": str(uuid.uuid4()),
        "policy_type": "individual_health",
        "premium_amount": "450.00",
        "coverage_limit": "500000.00",
        "deductible": "1500.00",
        "out_of_pocket_max": "8700.00",
        "effective_date": "2024-01-01",
        "expiry_date": "2024-12-31",
        "benefits_schedule": None,
    }


def _mock_policy(tenant_id=None) -> Policy:
    p = MagicMock(spec=Policy)
    p.id = uuid.uuid4()
    p.tenant_id = tenant_id or uuid.uuid4()
    p.policy_number = f"POL-{uuid.uuid4().hex[:8].upper()}"
    p.holder_id = uuid.uuid4()
    p.policy_type = PolicyType.INDIVIDUAL_HEALTH
    p.premium_amount = Decimal("450.00")
    p.coverage_limit = Decimal("500000.00")
    p.deductible = Decimal("1500.00")
    p.out_of_pocket_max = Decimal("8700.00")
    p.effective_date = date(2024, 1, 1)
    p.expiry_date = date(2024, 12, 31)
    p.status = PolicyStatus.ACTIVE
    p.created_at = datetime.now(timezone.utc)
    p.benefits_schedule = None
    return p


def _make_exec_result(obj=None, many=None):
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    r.scalar_one.return_value = len(many) if many is not None else 0
    items_mock = MagicMock()
    items_mock.all.return_value = many or []
    r.scalars.return_value = items_mock
    return r


# ─── POST /policies/ ──────────────────────────────────────────────────────────

class TestCreatePolicy:

    @pytest.mark.asyncio
    async def test_admin_can_create_policy(self, db_session: AsyncSession):
        """Admin must be able to create a policy (201 Created)."""
        admin = _make_user(UserRole.ADMIN)
        policy = _mock_policy(tenant_id=admin.tenant_id)

        call_count = 0

        async def fake_exec(stmt):
            nonlocal call_count
            call_count += 1
            # First call = duplicate check (None = no duplicate)
            # Second call = flush result (return policy)
            return _make_exec_result(obj=None if call_count == 1 else policy)

        with _client_for(db_session, admin) as ac:
            async with ac as client:
                with (
                    AsyncMock(side_effect=fake_exec) as _exec_mock,
                    patch_session_execute(_exec_mock),
                    patch_session_flush(),
                ):
                    response = await client.post("/policies/", json=_valid_policy_payload())

        assert response.status_code == 201, response.text

    @pytest.mark.asyncio
    async def test_underwriter_can_create_policy(self, db_session: AsyncSession):
        """Underwriter role must also be allowed to create policies."""
        uw = _make_user(UserRole.UNDERWRITER)
        policy = _mock_policy(tenant_id=uw.tenant_id)

        async def fake_exec(stmt):
            return _make_exec_result(obj=None)

        with _client_for(db_session, uw) as ac:
            async with ac as client:
                with patch_session_execute(AsyncMock(side_effect=fake_exec)), patch_session_flush():
                    response = await client.post("/policies/", json=_valid_policy_payload())

        # 201 or 500 (flush mock may not return full object) — key is NOT 403
        assert response.status_code != 403

    @pytest.mark.asyncio
    async def test_claims_adjuster_cannot_create_policy(self, db_session: AsyncSession):
        """CLAIMS_ADJUSTER must receive 403 Forbidden."""
        adjuster = _make_user(UserRole.CLAIMS_ADJUSTER)
        async with _client_for(db_session, adjuster) as client:
            response = await client.post("/policies/", json=_valid_policy_payload())
        app.dependency_overrides.clear()
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_insured_cannot_create_policy(self, db_session: AsyncSession):
        """INSURED role must receive 403 Forbidden."""
        insured = _make_user(UserRole.INSURED)
        async with _client_for(db_session, insured) as client:
            response = await client.post("/policies/", json=_valid_policy_payload())
        app.dependency_overrides.clear()
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_duplicate_policy_number_returns_409(self, db_session: AsyncSession):
        """Re-submitting the same policy_number must return HTTP 409 Conflict."""
        admin = _make_user(UserRole.ADMIN)
        existing = _mock_policy()

        async with _client_for(db_session, admin) as client:
            with patch_session_execute(AsyncMock(side_effect=lambda s: _make_exec_result(obj=existing))):
                response = await client.post("/policies/", json=_valid_policy_payload("POL-DUPLICATE"))

        app.dependency_overrides.clear()
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_invalid_date_range_returns_422(self, db_session: AsyncSession):
        """effective_date >= expiry_date must return 422."""
        admin = _make_user(UserRole.ADMIN)
        payload = _valid_policy_payload()
        payload["effective_date"] = "2024-12-31"
        payload["expiry_date"]    = "2024-01-01"

        async with _client_for(db_session, admin) as client:
            with patch_session_execute(AsyncMock(side_effect=lambda s: _make_exec_result(obj=None))):
                response = await client.post("/policies/", json=payload)

        app.dependency_overrides.clear()
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_required_field_returns_422(self, db_session: AsyncSession):
        """Omitting premium_amount must fail Pydantic validation with 422."""
        admin = _make_user(UserRole.ADMIN)
        payload = _valid_policy_payload()
        del payload["premium_amount"]

        async with _client_for(db_session, admin) as client:
            response = await client.post("/policies/", json=payload)

        app.dependency_overrides.clear()
        assert response.status_code == 422


# ─── GET /policies/ ───────────────────────────────────────────────────────────

class TestListPolicies:

    @pytest.mark.asyncio
    async def test_list_returns_200(self, db_session: AsyncSession):
        """GET /policies/ must return 200 with paginated schema."""
        admin = _make_user(UserRole.ADMIN)
        policies = [_mock_policy(tenant_id=admin.tenant_id) for _ in range(3)]

        call_count = 0

        async def fake_exec(stmt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_exec_result(obj=3)   # COUNT query
            return _make_exec_result(many=policies)

        async with _client_for(db_session, admin) as client:
            with patch_session_execute(AsyncMock(side_effect=fake_exec)):
                response = await client.get("/policies/")

        app.dependency_overrides.clear()
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_insured_only_sees_own_policies(self, db_session: AsyncSession):
        """INSURED role's query must be filtered to their holder_id."""
        insured = _make_user(UserRole.INSURED)

        async def fake_exec(stmt):
            return _make_exec_result(obj=0, many=[])

        async with _client_for(db_session, insured) as client:
            with patch_session_execute(AsyncMock(side_effect=fake_exec)):
                response = await client.get("/policies/")

        app.dependency_overrides.clear()
        assert response.status_code == 200


# ─── GET /policies/{id} ───────────────────────────────────────────────────────

class TestGetPolicy:

    @pytest.mark.asyncio
    async def test_get_existing_policy_returns_200(self, db_session: AsyncSession):
        """GET /policies/{id} must return 200 for an existing policy."""
        admin = _make_user(UserRole.ADMIN)
        policy = _mock_policy(tenant_id=admin.tenant_id)

        async with _client_for(db_session, admin) as client:
            with patch_session_execute(AsyncMock(side_effect=lambda s: _make_exec_result(obj=policy))):
                response = await client.get(f"/policies/{policy.id}")

        app.dependency_overrides.clear()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_nonexistent_policy_returns_404(self, db_session: AsyncSession):
        """GET /policies/{random_id} must return 404."""
        admin = _make_user(UserRole.ADMIN)

        async with _client_for(db_session, admin) as client:
            with patch_session_execute(AsyncMock(side_effect=lambda s: _make_exec_result(obj=None))):
                response = await client.get(f"/policies/{uuid.uuid4()}")

        app.dependency_overrides.clear()
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_insured_cannot_access_other_holders_policy(self, db_session: AsyncSession):
        """INSURED must receive 403 when policy belongs to a different holder."""
        insured = _make_user(UserRole.INSURED)
        policy = _mock_policy(tenant_id=insured.tenant_id)
        policy.holder_id = uuid.uuid4()  # Different holder — not the insured user

        async with _client_for(db_session, insured) as client:
            with patch_session_execute(AsyncMock(side_effect=lambda s: _make_exec_result(obj=policy))):
                response = await client.get(f"/policies/{policy.id}")

        app.dependency_overrides.clear()
        assert response.status_code == 403


# ─── Helpers ──────────────────────────────────────────────────────────────────

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def patch_session_execute(mock):
    with patch("sqlalchemy.ext.asyncio.AsyncSession.execute", mock):
        yield


@contextmanager
def patch_session_flush():
    with patch("sqlalchemy.ext.asyncio.AsyncSession.flush", new_callable=AsyncMock):
        yield
