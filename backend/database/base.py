"""
backend/database/base.py
────────────────────────
Async SQLAlchemy engine, session factory, and declarative base.

Design decisions:
• asyncpg driver selected for maximum PostgreSQL async throughput.
• pool_pre_ping=True ensures stale connections are recycled before use.
• AsyncSession factory with expire_on_commit=False avoids detached-instance
  errors when accessing attributes after a commit in async contexts.
• get_db() is an async generator, safe to use as a FastAPI Depends() target.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, MappedColumn
from sqlalchemy import MetaData

from backend.config import get_settings

logger = structlog.get_logger(__name__)

# ── Naming convention for Alembic auto-generate to produce deterministic names ──
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """All ORM models inherit from this base."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=settings.db_pool_pre_ping,
        # Return connections to the pool after each use
        pool_recycle=3600,
    )
    logger.info(
        "async_engine_created",
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    return engine


# Module-level singletons — created once at import time
engine: AsyncEngine = _build_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,   # Safe for async; avoids lazy-load DetachedInstanceError
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async database session.
    The session is committed on clean exit and rolled back on exception.

    Usage:
        @router.get("/example")
        async def example(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
