"""Database engine, session factory, declarative Base, and migration bootstrap."""

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

log = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    poolclass=NullPool,
    connect_args={
        "timeout": 10,         # seconds before giving up on a new connection
        "command_timeout": 30, # seconds before cancelling a hung query
    },
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Apply all *.sql migrations in order (idempotent — uses IF NOT EXISTS)."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    async with engine.begin() as conn:
        for f in files:
            sql = f.read_text()
            # asyncpg can't run multi-statement scripts in one call; split on ;
            for stmt in (s.strip() for s in sql.split(";") if s.strip()):
                await conn.execute(text(stmt))
            log.info("Applied migration: %s", f.name)

    # Seed RBAC system roles + link legacy admins (idempotent).
    from app.db import seed_rbac
    await seed_rbac.run()
    log.info("RBAC seed complete")
