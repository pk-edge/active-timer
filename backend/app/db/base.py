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
    # Per-environment env vars so each task stays under the DB max_connections,
    # leaving room for the 2x spike during a rolling deploy.
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=settings.DB_POOL_RECYCLE,
    connect_args={
        "timeout": 10,  # seconds before giving up on establishing a new connection
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
    """Apply all *.sql migrations in order, then run idempotent startup seeds."""
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

    # Create / repair the bootstrap admin from secrets (idempotent).
    # Best-effort: a seeding hiccup must NOT take down app startup.
    try:
        await _bootstrap_admin()
    except Exception:  # noqa: BLE001
        log.exception("Bootstrap admin failed (continuing startup)")


async def _bootstrap_admin() -> None:
    """Idempotently ensure the bootstrap admin exists with the configured password.

    Runs on every startup. No-op unless BOOTSTRAP_ADMIN_EMAIL + BOOTSTRAP_ADMIN_PASSWORD
    are set from the tracker/admin-{env} secret. Replaces the manual seedAdmin run-task
    and always sets role=admin + is_active=True so login works.
    """
    if not (settings.BOOTSTRAP_ADMIN_EMAIL and settings.BOOTSTRAP_ADMIN_PASSWORD):
        return

    from app.db.admin import ensure_admin

    async with AsyncSessionLocal() as db:
        _company, created = await ensure_admin(
            db,
            company_name=settings.BOOTSTRAP_COMPANY,
            email=settings.BOOTSTRAP_ADMIN_EMAIL,
            name=settings.BOOTSTRAP_ADMIN_NAME,
            password=settings.BOOTSTRAP_ADMIN_PASSWORD,
        )
        await db.commit()

    log.info(
        "Bootstrap admin %s: %s",
        "created" if created else "updated",
        settings.BOOTSTRAP_ADMIN_EMAIL.strip().lower(),
    )
