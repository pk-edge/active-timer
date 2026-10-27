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

def _make_engine(pool_size: int, max_overflow: int, statement_timeout_ms: int | None = None):
    connect_args: dict = {
        "timeout": 10,  # seconds before giving up on establishing a new connection
    }
    # Pin the session timezone to UTC on EVERY engine: report day-bucketing
    # (date_trunc / ::date / CURRENT_DATE) assumes UTC, and a server/role default
    # of another zone would silently shift those buckets.
    server_settings: dict[str, str] = {"timezone": "UTC"}
    if statement_timeout_ms:
        # Session-level ceiling on every statement from this pool. A runaway
        # analytics query can otherwise pin a pooled connection for minutes and
        # convoy every other request. Workers that legitimately need longer set
        # their own `SET LOCAL statement_timeout` inside their transaction, and
        # the migration runner resets it to 0 on its raw connections.
        server_settings["statement_timeout"] = str(statement_timeout_ms)
    connect_args["server_settings"] = server_settings
    return create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.DB_POOL_RECYCLE,
        # Fail fast: waiting the old 30s default just builds a convoy of stuck
        # requests when the pool is briefly exhausted — better a quick 500 on
        # the odd request than the whole app jammed for half a minute each.
        pool_timeout=settings.DB_POOL_TIMEOUT,
        # LIFO keeps a hot core of connections busy and lets the rest idle out
        # via pool_recycle — fewer open connections on small DB instances.
        pool_use_lifo=True,
        connect_args=connect_args,
    )


# Three ISOLATED pools so neither background ingest writes nor the singleton
# cache workers / report jobs can starve the connection an HTTP request needs
# for its auth lookup (and vice versa).
# Per-worker connection budget = (DB_POOL_SIZE + DB_MAX_OVERFLOW)
#                              + (INGEST_DB_POOL_SIZE + INGEST_DB_MAX_OVERFLOW)
#                              + (BACKGROUND_DB_POOL_SIZE + BACKGROUND_DB_MAX_OVERFLOW);
# keep 2x that under the DB max_connections to survive rolling deploys.
engine = _make_engine(settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW,
                      statement_timeout_ms=settings.DB_STATEMENT_TIMEOUT_MS)
ingest_engine = _make_engine(settings.INGEST_DB_POOL_SIZE, settings.INGEST_DB_MAX_OVERFLOW)
background_engine = _make_engine(settings.BACKGROUND_DB_POOL_SIZE, settings.BACKGROUND_DB_MAX_OVERFLOW,
                                 statement_timeout_ms=settings.DB_STATEMENT_TIMEOUT_MS)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Sessions for the ingest worker pool — bound to the dedicated writer engine.
IngestSessionLocal = async_sessionmaker(
    ingest_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Sessions for singleton background loops and report-job workers.
BackgroundSessionLocal = async_sessionmaker(
    background_engine,
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
    # Comment-only lines are removed BEFORE splitting: a ';' inside a comment
    # otherwise cuts the comment mid-line and the remainder is executed as SQL
    # (the "syntax error at or near ..." migration failures). Trailing inline
    # comments after code keep working as long as they contain no ';'.
    code_lines = [
        line for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    out = []
    for chunk in "\n".join(code_lines).split(";"):
        chunk = chunk.strip()
        if chunk:
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


# Arbitrary constant key for the cross-process migration/seed advisory lock.
_MIGRATION_LOCK_KEY = 0x7474_6D69_6772   # "ttmigr"
_MIGRATION_LOCK_POLL_SECS = 2.0


async def _migrate_and_seed() -> None:
    """Serialize migrations + seeds across workers/replicas, then run them.

    Every uvicorn worker (and every replica) runs this at boot. Without mutual
    exclusion two processes would apply the same pending file concurrently (DDL
    races, duplicate seeds). A Postgres SESSION advisory lock on a dedicated
    connection makes the first process do the work while the others poll
    `pg_try_advisory_lock` (NOT a blocking `pg_advisory_lock`: the pool's
    statement_timeout would cancel a long wait) and then find everything
    already applied — a no-op. The lock is always explicitly released and, if
    that fails, the connection is invalidated so a pooled connection can never
    carry the lock back into the pool. The lock connection is the only extra
    one held (pool = 3 + 2), the work itself uses the others.
    """
    waited = False
    async with engine.connect() as lock_conn:
        try:
            while True:
                got = (await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY}
                )).scalar()
                if got:
                    break
                if not waited:
                    log.info("Migrations: another process holds the migration lock — waiting")
                    waited = True
                await lock_conn.rollback()      # don't sit idle-in-transaction while polling
                await asyncio.sleep(_MIGRATION_LOCK_POLL_SECS)
            await lock_conn.rollback()
            await _migrate_and_seed_locked()
        finally:
            try:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})
                await lock_conn.rollback()
            except BaseException as exc:  # noqa: BLE001 — incl. cancellation at shutdown
                log.warning("Migrations: advisory unlock failed (%s) — invalidating connection",
                            type(exc).__name__)
                try:
                    await lock_conn.invalidate()   # server drops the session lock with the connection
                except Exception:  # noqa: BLE001
                    pass
                if not isinstance(exc, Exception):
                    raise


async def _migrate_and_seed_locked() -> None:
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

    # Always use the raw asyncpg connection for DDL statements.  SQLAlchemy's
    # text() parser treats :name tokens as bind parameters even inside SQL
    # comments, which breaks migrations whose comments reference query params
    # like :cid or :days.  The raw driver bypasses that parsing entirely.
    #
    # CONCURRENTLY builds additionally cannot run inside a transaction, so they
    # run autocommit.  Everything else runs in a single transaction so a failure
    # rolls back only that file.
    concurrent = "CONCURRENTLY" in sql.upper()
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection   # bare asyncpg Connection
        # Migrations (index builds, backfills) legitimately run long — lift the
        # engine's session-level statement_timeout for this connection.
        await driver.execute("SET statement_timeout = 0")
        if not concurrent:
            await driver.execute("SET lock_timeout = '5s'")
            tr = driver.transaction()
            await tr.start()
        try:
            for stmt in stmts:
                await driver.execute(stmt)
            if not concurrent:
                await tr.commit()
        except Exception:
            if not concurrent:
                await tr.rollback()
            raise

    # Record the applied file outside the DDL transaction so the record is
    # durable even when the migration contained autocommit statements.
    async with engine.begin() as conn:
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
