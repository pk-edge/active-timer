"""Database engine, session factory, declarative Base, and migration bootstrap."""

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

log = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    # ── Connection resilience ────────────────────────────────────────────────
    # On low-traffic environments (e.g. staging) idle pooled connections get
    # silently dropped by NAT gateways / firewalls / RDS idle-timeouts. The pool
    # still believes they are alive, so the next query is sent into a black hole
    # and hangs until the TCP timeout (~30s) before failing with a 500.
    #   • pool_pre_ping  — test a connection with SELECT 1 before handing it out;
    #                      a dead one is transparently discarded and replaced.
    #   • pool_recycle   — proactively retire connections older than 5 min, well
    #                      under the typical NAT/RDS idle cutoff, so they never go
    #                      stale in the first place. This is the real fix.
    #   • pool_timeout   — if the pool is genuinely exhausted, fail fast (10s)
    #                      instead of the 30s default.
    #   • connect_args   — bound asyncpg connect + per-query time so a bad
    #                      connection surfaces an error quickly rather than hanging.
    pool_pre_ping=True,
    pool_recycle=300,
    pool_timeout=10,
    connect_args={
        "timeout": 10,          # max seconds to establish a new connection
        "command_timeout": 30,  # max seconds for any single query
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
