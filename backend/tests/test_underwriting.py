"""
backend/tests/test_underwriting.py
────────────────────────────────────
Async tests for GET /underwriting/{app_id}/ai-summary/stream

Tests verify:
  • The endpoint streams valid SSE frames (token, citations, done)
  • Gemini client is properly mocked (no real API calls)
  • RAG retrieval is mocked to return fixture citations
  • Error frames are emitted on LLM failure (graceful degradation)
  • 404 for unknown application IDs

Run:
  pytest backend/tests/test_underwriting.py -v --asyncio-mode=auto
"""

from __future__ import annotations

import json
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.deps import get_current_user, get_db
from backend.database.models import Base, User, UserRole, Application, PolicyType, RiskTier, ApplicationStatus
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


def _mock_underwriter() -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.role = UserRole.UNDERWRITER
    user.is_active = True
    user.email = "underwriter@test.com"
    return user


def _mock_application(app_id: uuid.UUID | None = None) -> Application:
    """Build a mock Application ORM object for testing."""
    obj = MagicMock(spec=Application)
    obj.id = app_id or uuid.uuid4()
    obj.application_number = "APP-TEST-0001"
    obj.policy_type = PolicyType.INDIVIDUAL_HEALTH
    obj.requested_coverage_limit = 500_000
    obj.underwriting_score = 72
    obj.risk_tier = RiskTier.SUBSTANDARD
    obj.health_questionnaire = {
        "smoker": False,
        "pre_existing_conditions": True,
        "recent_surgery": False,
        "family_history_heart": True,
        "current_medications": True,
    }
    obj.ai_underwriting_notes = "Initial intake flagged T2DM."
    return obj


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    mock_user = _mock_underwriter()

    async def _db_gen():
        yield db_session

    app.dependency_overrides[get_db] = _db_gen
    app.dependency_overrides[get_current_user] = lambda: mock_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()


# ─── Mock SSE helpers ──────────────────────────────────────────────────────────

MOCK_TOKENS = ["The applicant ", "has Type 2 Diabetes. ", "Risk tier: Substandard."]

MOCK_CITATION = {
    "id": 1,
    "chunk_id": "chunk-abc-001",
    "document_name": "Benefits_Guide_2024.pdf",
    "document_url": "",
    "bounding_box": None,
    "excerpt": "Type 2 Diabetes coverage requires HbA1c within 90 days.",
    "rrf_score": 0.042,
    "cross_encoder_score": 0.87,
    "final_rank": 1,
}


def _make_mock_gemini(tokens=None):
    """Return a mock GeminiClient whose stream_complete yields fixture tokens."""
    tokens = tokens or MOCK_TOKENS

    async def _fake_stream(*args, **kwargs):
        for t in tokens:
            yield t

    mock = MagicMock()
    mock.stream_complete = _fake_stream
    return mock


def _parse_sse(content: bytes) -> list[dict]:
    """Parse raw SSE bytes into a list of decoded JSON frames."""
    frames = []
    for line in content.decode().splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                frames.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return frames


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestUnderwritingStreamEndpoint:

    @pytest.mark.asyncio
    async def test_stream_returns_200_for_valid_app(self, client: AsyncClient):
        """A valid application ID must return HTTP 200 with text/event-stream."""
        app_id = uuid.uuid4()
        mock_app = _mock_application(app_id)

        with patch("backend.api.routers.underwriting.get_llm_client", return_value=_make_mock_gemini()):
            with patch("backend.api.routers.underwriting._retrieve_citations", new_callable=AsyncMock, return_value=[MOCK_CITATION]):
                with patch(
                    "sqlalchemy.ext.asyncio.AsyncSession.execute",
                    new_callable=AsyncMock,
                    side_effect=lambda stmt: _mock_exec(mock_app),
                ):
                    response = await client.get(f"/underwriting/{app_id}/ai-summary/stream")

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_stream_emits_token_frames(self, client: AsyncClient):
        """The stream must emit at least one 'token' frame with content."""
        app_id = uuid.uuid4()
        mock_app = _mock_application(app_id)

        with patch("backend.api.routers.underwriting.get_llm_client", return_value=_make_mock_gemini()):
            with patch("backend.api.routers.underwriting._retrieve_citations", new_callable=AsyncMock, return_value=[]):
                with patch(
                    "sqlalchemy.ext.asyncio.AsyncSession.execute",
                    new_callable=AsyncMock,
                    side_effect=lambda stmt: _mock_exec(mock_app),
                ):
                    response = await client.get(f"/underwriting/{app_id}/ai-summary/stream")

        frames = _parse_sse(response.content)
        token_frames = [f for f in frames if f.get("type") == "token"]
        assert len(token_frames) == len(MOCK_TOKENS)
        assert token_frames[0]["content"] == MOCK_TOKENS[0]

    @pytest.mark.asyncio
    async def test_stream_emits_citations_frame(self, client: AsyncClient):
        """The stream must emit a 'citations' frame with real RAG results."""
        app_id = uuid.uuid4()
        mock_app = _mock_application(app_id)

        with patch("backend.api.routers.underwriting.get_llm_client", return_value=_make_mock_gemini()):
            with patch(
                "backend.api.routers.underwriting._retrieve_citations",
                new_callable=AsyncMock,
                return_value=[MOCK_CITATION],
            ):
                with patch(
                    "sqlalchemy.ext.asyncio.AsyncSession.execute",
                    new_callable=AsyncMock,
                    side_effect=lambda stmt: _mock_exec(mock_app),
                ):
                    response = await client.get(f"/underwriting/{app_id}/ai-summary/stream")

        frames = _parse_sse(response.content)
        citation_frames = [f for f in frames if f.get("type") == "citations"]
        assert len(citation_frames) == 1
        cits = citation_frames[0]["citations"]
        assert len(cits) == 1
        assert cits[0]["chunk_id"] == "chunk-abc-001"
        assert cits[0]["document_name"] == "Benefits_Guide_2024.pdf"

    @pytest.mark.asyncio
    async def test_stream_ends_with_done_frame(self, client: AsyncClient):
        """The final SSE frame must always be {'type': 'done'}."""
        app_id = uuid.uuid4()
        mock_app = _mock_application(app_id)

        with patch("backend.api.routers.underwriting.get_llm_client", return_value=_make_mock_gemini()):
            with patch("backend.api.routers.underwriting._retrieve_citations", new_callable=AsyncMock, return_value=[]):
                with patch(
                    "sqlalchemy.ext.asyncio.AsyncSession.execute",
                    new_callable=AsyncMock,
                    side_effect=lambda stmt: _mock_exec(mock_app),
                ):
                    response = await client.get(f"/underwriting/{app_id}/ai-summary/stream")

        frames = _parse_sse(response.content)
        assert frames, "Expected at least one SSE frame"
        assert frames[-1]["type"] == "done"

    @pytest.mark.asyncio
    async def test_stream_returns_404_for_missing_app(self, client: AsyncClient):
        """A request for a non-existent application ID must return HTTP 404."""
        with patch(
            "sqlalchemy.ext.asyncio.AsyncSession.execute",
            new_callable=AsyncMock,
            side_effect=lambda stmt: _mock_exec(None),
        ):
            response = await client.get(f"/underwriting/{uuid.uuid4()}/ai-summary/stream")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_stream_emits_error_frame_on_llm_failure(self, client: AsyncClient):
        """If the LLM raises, the stream must emit an 'error' frame (no 500 crash)."""
        app_id = uuid.uuid4()
        mock_app = _mock_application(app_id)

        # Mock a non-Gemini client (wrong provider triggers RuntimeError in _stream_gemini)
        bad_client = MagicMock()
        bad_client.__class__.__name__ = "OpenAIClient"  # not GeminiClient

        with patch("backend.api.routers.underwriting.get_llm_client", return_value=bad_client):
            with patch(
                "sqlalchemy.ext.asyncio.AsyncSession.execute",
                new_callable=AsyncMock,
                side_effect=lambda stmt: _mock_exec(mock_app),
            ):
                response = await client.get(f"/underwriting/{app_id}/ai-summary/stream")

        frames = _parse_sse(response.content)
        error_frames = [f for f in frames if f.get("type") == "error"]
        assert len(error_frames) >= 1
        assert "error" in error_frames[0]

    @pytest.mark.asyncio
    async def test_rag_failure_falls_back_to_empty_citations(self, client: AsyncClient):
        """If _retrieve_citations raises, the stream must still complete with citations: []."""
        app_id = uuid.uuid4()
        mock_app = _mock_application(app_id)

        with patch("backend.api.routers.underwriting.get_llm_client", return_value=_make_mock_gemini()):
            with patch(
                "backend.api.routers.underwriting._retrieve_citations",
                new_callable=AsyncMock,
                side_effect=Exception("ChromaDB connection refused"),
            ):
                with patch(
                    "sqlalchemy.ext.asyncio.AsyncSession.execute",
                    new_callable=AsyncMock,
                    side_effect=lambda stmt: _mock_exec(mock_app),
                ):
                    response = await client.get(f"/underwriting/{app_id}/ai-summary/stream")

        frames = _parse_sse(response.content)
        citation_frames = [f for f in frames if f.get("type") == "citations"]
        assert len(citation_frames) == 1
        assert citation_frames[0]["citations"] == []


class TestRAGQueryBuilder:
    """Unit tests for the clinical query builder (no HTTP, no DB)."""

    def test_smoker_flag_adds_tobacco_phrase(self):
        from backend.api.routers.underwriting import _build_rag_query
        app = _mock_application()
        app.health_questionnaire = {"smoker": True}
        query = _build_rag_query(app)
        assert "tobacco" in query.lower() or "nicotine" in query.lower()

    def test_no_conditions_produces_standard_risk_query(self):
        from backend.api.routers.underwriting import _build_rag_query
        app = _mock_application()
        app.health_questionnaire = {}
        query = _build_rag_query(app)
        assert "standard risk" in query.lower() or "no significant" in query.lower()

    def test_coverage_limit_appears_in_query(self):
        from backend.api.routers.underwriting import _build_rag_query
        app = _mock_application()
        app.requested_coverage_limit = 750_000
        query = _build_rag_query(app)
        assert "750,000" in query or "750000" in query


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_exec(obj):
    result = MagicMock()
    result.scalar_one_or_none.return_value = obj
    return result
