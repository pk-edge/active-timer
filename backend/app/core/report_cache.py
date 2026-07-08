"""
Report Cache — background worker that pre-computes company-level report
aggregates so the two heaviest endpoints never run live GROUP BY scans.

Architecture
────────────
                  ┌─ admin request ──► GET /company/summary
                  │                       │
                  │         check report_daily_summary (fast PK lookup)
                  │                       │
                  │         hit  ──► return rows   (~1 ms)
                  │         miss ──► live query    (fallback, ~150 ms+)
                  │
                  └─ worker schedule:
                       FAST (every 30 min):
                         ├─ daily_summary incremental — scan only today+yesterday
                         └─ touch refreshed_at on older rows (no scan)
                       SLOW (every 4th fast cycle = every 2 h):
                         └─ top_apps for each window (7 / 14 / 30 days)

Who benefits
────────────
Only full-scope admins (scope='all') — those whose company_summary query scans
ALL company devices. Scoped users (scope='own', scope='groups') fall back to
live queries because their result is a device-specific subset the cache does not
store.

Staleness
─────────
Daily summary rows are refreshed every 30 min (incremental); STALE_MINUTES_DAILY
(35) is the guard so stale data is never surfaced.
Top-apps rows are refreshed every 2 h; STALE_MINUTES_TOP_APPS (130) matches.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

log = logging.getLogger(__name__)

REFRESH_INTERVAL     = 10 * 60   # seconds between fast (daily-summary) refreshes.
                                 # 10 min: each fast cycle is now day-chunked and
                                 # index-only (~seconds), so a tight cadence keeps
                                 # the home dashboard "today" numbers near-live at
                                 # negligible DB cost.
SLOW_EVERY_N         = 12        # run top-apps refresh every N fast cycles (12×10min=2h)
STALE_MINUTES_DAILY  = 35        # max age for daily-summary rows
STALE_MINUTES_TOP_APPS = 130     # max age for top-apps rows (aligns with 2-h slow cycle)

_WINDOWS         = (7, 14, 30)   # days_window values to pre-compute for top-apps
_slow_cycle: int = 0             # counts fast cycles; reset to 0 after each slow run

# Must stay in sync with home._SYSTEM_IDLE_APPS — includes the macOS lock/login
# screens (loginwindow, ScreenSaverEngine) so cached "active" totals match the
# live dashboard formula.
_IDLE_APPS_SQL = (
    "('loginwindow','ScreenSaverEngine',"
    "'System Idle Process','LockApp','LogonUI','Idle','screensaver')"
)

_worker_task: asyncio.Task | None = None


# ── Refresh ──────────────────────────────────────────────────────────────────

async def _tables_exist(db) -> bool:
    """Return False when the migration hasn't run yet (e.g. on first boot before
    the background migration task has created the tables)."""
    from sqlalchemy import text
    row = await db.execute(text("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name   IN ('report_daily_summary', 'report_daily_device_cache')
    """))
    return row.scalar() == 2


async def _refresh_daily_summary(*, is_full: bool) -> None:
    """
    Incremental daily-summary refresh, chunked ONE DAY per transaction.

    First run (empty table): backfills 31 days — covers the max 30-day query
    window. Subsequent runs: only today + yesterday (the only days that can have
    changed since last run), then cheaply touch refreshed_at on older rows.

    Day-chunking matters: a single multi-day GROUP BY over a 40M+-row events
    table exceeds any sane statement timeout and holds a heavy scan open for
    minutes. One day (~1-2M rows) aggregates in seconds via the partial covering
    indexes, each day commits independently, and the loop yields between days so
    ingest never starves.
    """
    from app.db.base import AsyncSessionLocal
    from sqlalchemy import text

    days_back = 31 if is_full else 2
    log.info("report_cache: daily-summary refresh (%d day(s), chunked)", days_back)

    for offset in range(days_back):
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET LOCAL statement_timeout = '120000'"))
            # The window/idle split lets each half run as an index-only scan on
            # its partial covering index instead of one bitmap heap scan over
            # every event type (which is what used to time out).
            await db.execute(text(f"""
                INSERT INTO report_daily_summary
                    (company_id, day, active_seconds, idle_seconds, device_count, refreshed_at)
                SELECT
                    company_id,
                    day,
                    SUM(active_seconds) AS active_seconds,
                    SUM(idle_seconds)   AS idle_seconds,
                    COUNT(DISTINCT device_id) AS device_count,
                    NOW() AS refreshed_at
                FROM (
                    SELECT d.company_id,
                           DATE(ae.timestamp) AS day,
                           ae.device_id,
                           CASE WHEN ae.app_name NOT IN {_IDLE_APPS_SQL}
                                THEN ae.duration ELSE 0 END AS active_seconds,
                           0 AS idle_seconds
                    FROM activity_events ae
                    JOIN devices d ON d.id = ae.device_id
                    WHERE ae.event_type = 'window_change'
                      AND ae.app_name IS NOT NULL
                      AND ae.timestamp >= CURRENT_DATE - CAST(:offset AS integer)
                      AND ae.timestamp <  CURRENT_DATE - CAST(:offset AS integer) + INTERVAL '1 day'
                    UNION ALL
                    SELECT d.company_id,
                           DATE(ae.timestamp) AS day,
                           ae.device_id,
                           0,
                           ae.duration
                    FROM activity_events ae
                    JOIN devices d ON d.id = ae.device_id
                    WHERE ae.event_type = 'idle_start'
                      AND ae.timestamp >= CURRENT_DATE - CAST(:offset AS integer)
                      AND ae.timestamp <  CURRENT_DATE - CAST(:offset AS integer) + INTERVAL '1 day'
                ) u
                GROUP BY company_id, day
                ON CONFLICT (company_id, day) DO UPDATE SET
                    active_seconds = EXCLUDED.active_seconds,
                    idle_seconds   = EXCLUDED.idle_seconds,
                    device_count   = EXCLUDED.device_count,
                    refreshed_at   = EXCLUDED.refreshed_at
            """), {"offset": offset})

            # Per-device breakdown for the same day — powers the home widgets
            # "active today / avg active per device" without touching events.
            await db.execute(text(f"""
                INSERT INTO report_daily_device_cache
                    (company_id, day, device_id, active_seconds, refreshed_at)
                SELECT
                    d.company_id,
                    DATE(ae.timestamp) AS day,
                    ae.device_id,
                    SUM(CASE WHEN ae.app_name NOT IN {_IDLE_APPS_SQL}
                             THEN ae.duration ELSE 0 END) AS active_seconds,
                    NOW() AS refreshed_at
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                WHERE ae.event_type = 'window_change'
                  AND ae.app_name IS NOT NULL
                  AND ae.timestamp >= CURRENT_DATE - CAST(:offset AS integer)
                  AND ae.timestamp <  CURRENT_DATE - CAST(:offset AS integer) + INTERVAL '1 day'
                GROUP BY d.company_id, DATE(ae.timestamp), ae.device_id
                ON CONFLICT (company_id, day, device_id) DO UPDATE SET
                    active_seconds = EXCLUDED.active_seconds,
                    refreshed_at   = EXCLUDED.refreshed_at
            """), {"offset": offset})
            await db.commit()
        await asyncio.sleep(0)  # yield between days so ingest writes get a turn

    if not is_full:
        # Refresh the staleness timestamp on historical rows without
        # re-scanning activity_events.  Both tables are tiny
        # (companies × days [× devices]), so these UPDATEs are negligible.
        async with AsyncSessionLocal() as db:
            for tbl in ("report_daily_summary", "report_daily_device_cache"):
                await db.execute(text(f"""
                    UPDATE {tbl}
                    SET    refreshed_at = NOW()
                    WHERE  day <  CURRENT_DATE - 1
                      AND  day >= CURRENT_DATE - 31
                """))
            await db.commit()


async def _refresh_top_apps() -> None:
    """
    Full top-apps refresh for each window — runs every 2 hours.
    Each window gets its own session + transaction so a failure in one
    window cannot roll back the others.
    """
    from app.db.base import AsyncSessionLocal
    from sqlalchemy import text
    log.info("report_cache: top-apps refresh start (windows: %s)", _WINDOWS)
    for window in _WINDOWS:
        async with AsyncSessionLocal() as db:
            # 300 s: the 30-day window is an index-only scan over the partial
            # covering index, but on 40M+ rows it can still take minutes cold.
            await db.execute(text("SET LOCAL statement_timeout = '300000'"))
            await db.execute(text(f"""
                INSERT INTO report_top_apps_cache
                    (company_id, days_window, app_name, seconds, refreshed_at)
                SELECT
                    d.company_id,
                    {window}                         AS days_window,
                    normalize_app_name(ae.app_name)  AS app_name,
                    SUM(ae.duration)                 AS seconds,
                    NOW()                            AS refreshed_at
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                WHERE ae.event_type = 'window_change'
                  AND ae.app_name IS NOT NULL
                  AND ae.app_name NOT IN {_IDLE_APPS_SQL}
                  AND ae.timestamp >= NOW() - make_interval(days => :window)
                GROUP BY d.company_id, normalize_app_name(ae.app_name)
                ON CONFLICT (company_id, days_window, app_name) DO UPDATE SET
                    seconds      = EXCLUDED.seconds,
                    refreshed_at = EXCLUDED.refreshed_at
            """), {"window": window})
            await db.commit()
        # yield the event loop between windows so ingest writes get a turn
        await asyncio.sleep(0)
    log.info("report_cache: top-apps refresh complete")


async def _do_refresh(*, run_slow: bool) -> None:
    try:
        from app.db.base import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            if not await _tables_exist(db):
                log.info("report_cache: tables not ready yet — skipping refresh")
                return
            has_row = await db.execute(text(
                "SELECT 1 FROM report_daily_summary LIMIT 1"
            ))
            is_full = has_row.fetchone() is None

        await _refresh_daily_summary(is_full=is_full)

        if run_slow:
            await _refresh_top_apps()

        log.info("report_cache: refresh cycle complete (slow=%s)", run_slow)
    except Exception:
        log.exception("report_cache: refresh failed")


async def _loop() -> None:
    global _slow_cycle
    # First boot: populate everything
    await _do_refresh(run_slow=True)
    _slow_cycle = 0

    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        _slow_cycle += 1
        run_slow = (_slow_cycle >= SLOW_EVERY_N)
        await _do_refresh(run_slow=run_slow)
        if run_slow:
            _slow_cycle = 0


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def start() -> None:
    global _worker_task
    _worker_task = asyncio.create_task(_loop(), name="report_cache")
    log.info(
        "report_cache: worker started (fast_interval=%ds, slow_every=%d cycles)",
        REFRESH_INTERVAL, SLOW_EVERY_N,
    )


async def stop() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    log.info("report_cache: worker stopped")


# ── Cache reads ───────────────────────────────────────────────────────────────

async def summary_from_cache(db, company_id: UUID, days: int):
    """Return fresh cached daily-summary rows, or None on miss / stale / table absent."""
    from sqlalchemy import text
    try:
        # CAST(:days AS integer) is required: with an untyped parameter asyncpg
        # lets Postgres infer `CURRENT_DATE - $n` as date-date, which then fails
        # with "operator does not exist: date >= integer" on EVERY call.
        result = await db.execute(text("""
            SELECT day, active_seconds, idle_seconds, device_count
            FROM   report_daily_summary
            WHERE  company_id   = :cid
              AND  day          >= CURRENT_DATE - CAST(:days AS integer)
              AND  refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
            ORDER BY day DESC
        """), {"cid": str(company_id), "days": days, "stale": STALE_MINUTES_DAILY})
        rows = result.fetchall()
        return rows if rows else None
    except Exception:
        log.debug("report_cache.summary_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def top_apps_from_cache(db, company_id: UUID, days: int, limit: int):
    """Return fresh cached top-app rows, or None on miss / stale / non-standard window / table absent.

    Only pre-computed for days in (7, 14, 30). Any other value returns None so
    the endpoint falls back to the live query.
    """
    if days not in _WINDOWS:
        return None
    from sqlalchemy import text
    try:
        result = await db.execute(text("""
            SELECT app_name, seconds
            FROM   report_top_apps_cache
            WHERE  company_id   = :cid
              AND  days_window  = :window
              AND  refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
            ORDER BY seconds DESC
            LIMIT  :limit
        """), {"cid": str(company_id), "window": days, "limit": limit, "stale": STALE_MINUTES_TOP_APPS})
        rows = result.fetchall()
        return rows if rows else None
    except Exception:
        log.debug("report_cache.top_apps_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def device_active_from_cache(db, company_id, since_dt, until_dt):
    """Return fresh per-device active seconds over [since, until] as
    {device_id_str: seconds}, or None on miss / stale / table absent.

    Returning an empty dict is a valid hit (no device was active); staleness is
    detected via the freshness-guarded count, so callers can trust {}.
    """
    from sqlalchemy import text
    try:
        fresh = (await db.execute(text("""
            SELECT COUNT(*) FROM report_daily_summary
            WHERE company_id   = :cid
              AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
        """), {"cid": str(company_id), "stale": STALE_MINUTES_DAILY})).scalar()
        if not fresh:
            return None

        result = await db.execute(text("""
            SELECT device_id, SUM(active_seconds) AS active
            FROM   report_daily_device_cache
            WHERE  company_id   = :cid
              AND  day          BETWEEN :since AND :until
              AND  refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
            GROUP BY device_id
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt,
               "stale": STALE_MINUTES_DAILY})
        return {str(r.device_id): int(r.active or 0) for r in result.fetchall()}
    except Exception:
        log.debug("report_cache.device_active_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None
