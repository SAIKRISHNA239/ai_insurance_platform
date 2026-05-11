"""
backend/main.py
───────────────
FastAPI Application Gateway — the single entry point for all HTTP traffic.

Responsibilities:
  1. Lifespan context: manages async DB engine startup/teardown and
     ChromaDB collection initialization.
  2. Middleware stack: CORS, GZip, JWT auth, RBAC.
  3. Router registration: all domain routers mounted under /api/v1.
  4. Global exception handlers: structured JSON error responses.
  5. Health check endpoint: lightweight readiness probe for Docker / k8s.

Architecture notes:
  • No business logic lives here — this is purely an API gateway.
  • All async operations use the event loop provided by Uvicorn's ASGI server.
  • Middleware is applied in reverse order of declaration (Starlette convention).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from backend.config import get_settings
from backend.database.base import engine
from backend.database.models import Base
from backend.database.vector_client import get_chroma_client
from backend.middleware.auth import JWTAuthMiddleware, RBACMiddleware

# ── Routers ────────────────────────────────────────────────────────────────────
from backend.api.routers import auth, claims, policies, applications, underwriting

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — startup & shutdown hooks
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Async context manager called once at startup and once at shutdown.
    Use this for connection pool warm-up, migration checks, and clean teardown.
    """
    settings = get_settings()
    logger.info("application_starting", env=settings.app_env)

    # ── Startup ──────────────────────────────────────────────────────────────
    # 1. Verify PostgreSQL connectivity (the pool will lazy-connect on first use,
    #    but we do a cheap connect() to fail fast if misconfigured)
    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        logger.info("postgres_connected")
    except Exception as exc:
        logger.error("postgres_connection_failed", error=str(exc))
        raise

    # 2. Verify ChromaDB connectivity
    try:
        chroma = get_chroma_client()
        await chroma.heartbeat()
        logger.info("chromadb_connected")
    except Exception as exc:
        # ChromaDB is non-critical at startup — log warning and continue
        logger.warning("chromadb_heartbeat_failed", error=str(exc))

    logger.info("application_ready")
    yield  # ← application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("application_shutting_down")
    await engine.dispose()
    logger.info("postgres_pool_disposed")


# ─────────────────────────────────────────────────────────────────────────────
# Application Factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=(
            "AI-Powered Healthcare Insurance Intelligence Platform API.\n\n"
            "Provides structured endpoints for claims adjudication, policy management, "
            "and AI-driven underwriting. All endpoints require JWT authentication "
            "except /health, /auth/register, and /auth/token."
        ),
        version="1.0.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Middleware (applied in reverse declaration order by Starlette) ──────
    # Order of execution: GZip → CORS → JWT → RBAC → Routers
    app.add_middleware(RBACMiddleware)
    app.add_middleware(JWTAuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [],  # Restrict in prod
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # ── Request timing middleware (lightweight, no external dependency) ─────
    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"
        return response

    # ── Exception handlers ─────────────────────────────────────────────────
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning("request_validation_error", errors=exc.errors(), path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Request validation failed.",
                "errors": exc.errors(),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An internal server error occurred."},
        )

    # ── API Routers — all prefixed under /api/v1 ───────────────────────────
    API_PREFIX = "/api/v1"
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(claims.router, prefix=API_PREFIX)
    app.include_router(policies.router, prefix=API_PREFIX)
    app.include_router(applications.router, prefix=API_PREFIX)
    app.include_router(underwriting.router, prefix=API_PREFIX)

    # ── Health endpoint ────────────────────────────────────────────────────
    @app.get(
        "/health",
        tags=["Infrastructure"],
        summary="Application health probe",
        response_model=dict[str, Any],
    )
    async def health() -> dict[str, Any]:
        """
        Lightweight health check used by Docker HEALTHCHECK and load balancers.
        Does not perform a DB query — that would add latency to every k8s probe.
        """
        return {
            "status": "healthy",
            "version": "1.0.0",
            "service": settings.app_name,
        }

    return app


# ── Module-level app instance (used by Uvicorn CMD) ───────────────────────────
app = create_app()
