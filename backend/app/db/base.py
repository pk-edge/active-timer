"""Database engine, session factory, declarative Base, and migration bootstrap."""

import asyncio
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


async def _wait_for_db(retries: int = 15, delay: float = 2.0) -> None:
    """Retry connectivity before touching the schema. A DB that is briefly
    overloaded during a deploy must not insta-crash the new task into a
    restart loop (each restart adds yet more load)."""
    for attempt in range(1, retries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception:  # noqa: BLE001
            if attempt == retries:
                raise
            log.warning("DB not reachable (attempt %d/%d) — retrying in %.0fs",
                        attempt, retries, delay)
            await asyncio.sleep(delay)


async def _statements(sql: str):
    # asyncpg can't run multi-statement scripts in one call; split on ;
    # Chunks holding only comments/whitespace are dropped — a ';' inside a
    # comment line otherwise yields an empty statement that asyncpg rejects.
    out = []
    for chunk in sql.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        has_sql = any(
            line.strip() and not line.strip().startswith("--")
            for line in chunk.splitlines()
        )
        if has_sql:
            out.append(chunk)
    return out


# Keep a reference so the background migration task is never garbage-collected.
_startup_task: asyncio.Task | None = None


async def init_db() -> None:
    """Verify DB connectivity, then run migrations + seeds in the BACKGROUND.

    The app must bind and answer health checks immediately: on a large
    production database the first run of the perf-index migrations takes
    minutes, and awaiting them in lifespan means the LB health check kills
    the task mid-migration — ECS keeps the old task serving stale code and
    the deploy crash-loops without ever finishing. Routes ship instantly;
    schema work converges in the background (and resumes across restarts
    via schema_migrations).
    """
    global _startup_task
    await _wait_for_db()
    _startup_task = asyncio.create_task(_migrate_and_seed())


async def _migrate_and_seed() -> None:
    """Apply pending *.sql migrations, then run idempotent startup seeds.

    Applied files are recorded in schema_migrations so a restart executes ZERO
    DDL — re-running CREATE INDEX / backfill statements against a large, busy
    database on every boot is what stalls startup (lock waits → health-check
    timeout → task killed → restart loop). Each pending file runs in its own
    short transaction with a fail-fast lock_timeout; files containing
    CONCURRENTLY run on an autocommit connection (Postgres forbids them inside
    a transaction) so index builds never block ingest writes. A failing file
    is logged and skipped (NOT recorded — retried next boot); one bad
    migration must never block shipping unrelated routes.
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        ))
        applied = {
            r[0] for r in (await conn.execute(text("SELECT filename FROM schema_migrations"))).all()
        }

    pending = [f for f in files if f.name not in applied]
    if not pending:
        log.info("Migrations up to date (%d applied)", len(files))
    for f in pending:
        try:
            await _apply_migration(f)
            log.info("Applied migration: %s", f.name)
        except Exception:  # noqa: BLE001
            log.exception("Migration %s FAILED — skipped this boot, will retry "
                          "on next startup; investigate immediately", f.name)

    # Seed RBAC system roles + link legacy admins (idempotent, batched).
    # Best-effort: a seeding hiccup must NOT take down app startup.
    from app.db import seed_rbac

    try:
        await seed_rbac.run()
        log.info("RBAC seed complete")
    except Exception:  # noqa: BLE001
        log.exception("RBAC seed failed (continuing startup)")

    # Create / repair the bootstrap admin from secrets (idempotent).
    try:
        await _bootstrap_admin()
    except Exception:  # noqa: BLE001
        log.exception("Bootstrap admin failed (continuing startup)")


async def _apply_migration(f: Path) -> None:
    sql = f.read_text()
    stmts = await _statements(sql)

    if "CONCURRENTLY" in sql.upper():
        # Statement by statement on the RAW asyncpg connection: CONCURRENTLY
        # builds take no write-blocking lock (agents keep ingesting), Postgres
        # forbids them inside a transaction, and the SQLAlchemy cursor layer is
        # flaky here — its rowcount regex raises TypeError when the driver
        # returns no status message for concurrent DDL (seen on first boot).
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            for stmt in stmts:
                await raw.driver_connection.execute(stmt)
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f) ON CONFLICT DO NOTHING"),
                {"f": f.name},
            )
    else:
        # One transaction per file: short lock windows, and a failure rolls
        # back only this file. lock_timeout fails fast instead of queueing
        # behind live ingest writes and stalling the whole startup.
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            for stmt in stmts:
                await conn.execute(text(stmt))
            await conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f) ON CONFLICT DO NOTHING"),
                {"f": f.name},
            )


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
