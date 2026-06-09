"""Database engine, session factory, declarative Base, and migration bootstrap."""

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from app.core.config import get_settings

log = logging.getLogger(__name__)

settings = get_settings()

# Pool sizing — kept small so we never approach Postgres max_connections.
# Overflow lets us absorb short bursts without exhausting the base pool.
# Both values are intentionally conservative; raise them if profiling shows
# connection wait time under sustained prod load.
_POOL_SIZE     = 5   # persistent warm connections
_MAX_OVERFLOW  = 10  # burst headroom  →  15 total max

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    # ── The two fixes that prevent pool exhaustion ────────────────────────────
    # 1. pool_recycle: AWS NAT gateway silently kills idle TCP connections at
    #    ~350 s. Recycling at 120 s means connections are replaced well before
    #    the NAT kills them, so the pool never holds stale sockets.
    # 2. pool_timeout: if all slots are taken, fail the request in 5 s instead
    #    of the default 30 s. A 30 s wait cascades — every new request queues
    #    behind the stuck ones, the pool stays full for 30 s, and dozens of
    #    requests pile up. 5 s stops the cascade early.
    pool_recycle=120,
    pool_timeout=5,
    # pre-ping catches any connection the recycle window missed (e.g. a DB
    # restart mid-cycle). The SELECT 1 costs ~0.1 ms on a LAN — negligible.
    pool_pre_ping=True,
    # asyncpg-level caps so a network blip never hangs indefinitely.
    connect_args={
        "timeout": 10,         # max seconds to open a new connection
        "command_timeout": 30, # max seconds for any single query
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
