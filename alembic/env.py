"""
alembic/env.py
───────────────
Alembic async migration environment.

Uses SQLAlchemy's async engine with run_sync() to execute the
synchronous Alembic migration logic inside an async context.
All ORM models are imported here so Alembic can detect schema changes.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# ── Import all models so Alembic sees them for autogenerate ───────────────────
# CRITICAL: Any model not imported here will be ignored by 'alembic revision --autogenerate'
from backend.database.base import Base
from backend.database import models  # noqa: F401 — registers all ORM models

from backend.config import get_settings

config = context.config
settings = get_settings()

# Override the sqlalchemy.url from alembic.ini with the env-resolved value
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection required)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using an async engine."""
    async_engine = create_async_engine(settings.database_url, echo=False)

    async with async_engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await async_engine.dispose()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
