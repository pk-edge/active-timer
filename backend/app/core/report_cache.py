"""
Report Cache — background worker that pre-computes per-day report aggregates so
the Reports page answers ANY picker range (up to 2 years) from small tables
instead of aggregating activity_events live.

Tables (one row set per company/day, all filled together by _compute_day)
─────────────────────────────────────────────────────────────────────────
  report_daily_summary       company totals: active / idle / device_count
  report_daily_device_cache  per device: active / idle (a row = the device logged
                             a window_change or idle_start that day)
  report_daily_app_usage     per normalized app: seconds (window_change only,
                             idle/lock-screen processes excluded)
  report_cache_state         ONE row ('global'): covered_since + last_fast_refresh

Coverage contract
─────────────────
Every day >= covered_since is fully computed in all three tables for every
company. Readers answer from the tables only when the requested range starts on
or after covered_since AND (the range stays clear of today/yesterday OR the fast
cycle ran within STALE_MINUTES_DAILY). Anything else returns None and the caller
runs the live query. home.py's two readers (summary_from_cache,
device_active_from_cache) gate on the same two facts: last_fast_refresh within
STALE_MINUTES_DAILY and the requested days >= covered_since — nothing per row.

Late-arriving data
──────────────────
Agents buffer offline and upload days later, so a day older than yesterday is
NOT final. The leader therefore re-runs _compute_day (idempotent) for settled
days on a schedule:
  * every TRAILING_INTERVAL (1 h): days 2..TRAILING_DAYS (7) back
  * once per UTC day:              days TRAILING_DAYS+1..DEEP_DAYS (8..30) back
Guarantee: events that reach the database up to 7 days late are reflected in the
cache within <= 1 h of ingest, up to 30 days late within <= 24 h (plus the
bounded-work slack below). Data later than 30 days is out of scope (the live
paths include it, the cache does not). One day per transaction, bounded per
cycle by LATE_BUDGET_SECS; unfinished days resume next cycle.

Worker schedule
───────────────
  FAST (REFRESH_INTERVAL): recompute today + yesterday (and any days since the
      previous run if the worker was down), stamp last_fast_refresh. First ever
      run on an empty summary table fills 31 days (the home dashboard reads it).
  LATE (hourly / daily): re-run settled days 2..30 back for late-arriving agent
      data (see above), on the background pool, one day per transaction.
  BACKFILL (every cycle until done): extend covered_since backwards by at most
      BACKFILL_DAYS_PER_CYCLE days / BACKFILL_BUDGET_SECS seconds, one
      transaction per day, until COVER_DAYS days back. While work remains the
      loop wakes every BACKFILL_INTERVAL instead of REFRESH_INTERVAL.
  RETENTION (once per UTC day): drop rows older than RETENTION_DAYS.

Who benefits
────────────
Full-scope admins (company summary + top apps), scoped users (summary via the
per-device table), the device summary, and the fleet / device_full exports.
Scoped top-apps stays live (a few devices, covering index).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

log = logging.getLogger(__name__)

# Every DB session in this module's loops comes from the isolated BACKGROUND
# pool (app.db.base.BackgroundSessionLocal), never the HTTP pool: a backfill or
# a long day recompute must not take the leader worker's request connections.

REFRESH_INTERVAL     = 10 * 60   # seconds between fast refreshes (today + yesterday).
                                 # Each fast cycle is two index-only day scans, so a
                                 # tight cadence keeps "today" near-live at negligible
                                 # DB cost.
BACKFILL_INTERVAL    = 90        # seconds between cycles while the horizon is still
                                 # extending backwards
STALE_MINUTES_DAILY  = 35        # max age of last_fast_refresh for a range that
                                 # includes today/yesterday

COVER_DAYS           = 732       # horizon target. Must be >= reports.MAX_REPORT_RANGE_DAYS
RETENTION_DAYS       = 740       # rows older than this are deleted (a few days beyond
                                 # COVER_DAYS so the horizon never races the delete)
BACKFILL_DAYS_PER_CYCLE = 4      # max days one cycle adds to the horizon
BACKFILL_BUDGET_SECS    = 60     # ... and stop starting new days after this long
MAX_CATCHUP_DAYS     = 7         # fast cycle recomputes at most this many days back
                                 # after downtime; a longer outage lowers the horizon
DAY_STATEMENT_TIMEOUT_MS = 120_000

TRAILING_DAYS        = 7         # days 2..7 back are re-run every TRAILING_INTERVAL
TRAILING_INTERVAL    = 60 * 60   # seconds between trailing-window recomputes
DEEP_DAYS            = 30        # days 8..30 back are re-run once per UTC day
LATE_BUDGET_SECS     = 120       # per cycle: stop starting new late days after this
LATE_STAGGER_SECS    = 2.0       # pause between late days so ingest gets a turn
BG_CHECKOUT_WAIT_SECS = 120      # how long a loop waits for a background-pool connection

_STATE_NAME      = "global"
_DAY_LOCK_KEY    = 727310001     # pg_advisory_xact_lock: serialises per-day recomputes
                                 # across workers/processes (DELETE+INSERT is not
                                 # safe to interleave)

# Must stay in sync with home._SYSTEM_IDLE_APPS — includes the macOS lock/login
# screens (loginwindow, ScreenSaverEngine) so cached "active" totals match the
# live dashboard formula.
_IDLE_APPS_SQL = (
    "('loginwindow','ScreenSaverEngine',"
    "'System Idle Process','LockApp','LogonUI','Idle','screensaver')"
)

_worker_task: asyncio.Task | None = None
_last_fast_mono: float | None = None
_last_retention_day: date | None = None
_last_trailing_mono: float | None = None
_last_deep_day: date | None = None
_late_pending: list[date] = []     # settled days still to re-run, in order


@asynccontextmanager
async def background_session():
    """A BackgroundSessionLocal session whose connection is checked out up front,
    retrying while the (tiny, shared) background pool is busy. A pool-checkout
    timeout there only means another background job holds the connection, so
    wait for it (up to BG_CHECKOUT_WAIT_SECS) instead of failing the cycle."""
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from app.db.base import BackgroundSessionLocal
    deadline = time.monotonic() + BG_CHECKOUT_WAIT_SECS
    while True:
        session = BackgroundSessionLocal()
        try:
            await session.connection()
            break
        except PoolTimeout:
            await session.close()
            if time.monotonic() >= deadline:
                raise
        except BaseException:
            await session.close()
            raise
    try:
        yield session
    finally:
        await session.close()


# ── Range-reader SQL (shared with the export builders) ───────────────────────
# Unordered: callers sort, because the on-screen endpoints list newest-first and
# the exports oldest-first. Column aliases match the live queries in
# app/services/ReportQueries.py so rows from either source are interchangeable.

SUMMARY_RANGE_SQL = """
    SELECT day,
           active_seconds AS active,
           idle_seconds   AS idle,
           device_count
    FROM   report_daily_summary
    WHERE  company_id = :cid
      AND  day BETWEEN CAST(:since AS date) AND CAST(:until AS date)
"""

DEVICE_SUMMARY_RANGE_SQL = """
    SELECT day,
           SUM(active_seconds) AS active,
           SUM(idle_seconds)   AS idle,
           COUNT(*)            AS device_count
    FROM   report_daily_device_cache
    WHERE  device_id = ANY(:vids)
      AND  company_id = :cid
      AND  day BETWEEN CAST(:since AS date) AND CAST(:until AS date)
    GROUP  BY day
"""

APPS_RANGE_SQL = """
    SELECT app_name, SUM(seconds) AS seconds
    FROM   report_daily_app_usage
    WHERE  company_id = :cid
      AND  day BETWEEN CAST(:since AS date) AND CAST(:until AS date)
    GROUP  BY app_name
    ORDER  BY seconds DESC, app_name
    LIMIT  :limit
"""


# ── Refresh ──────────────────────────────────────────────────────────────────

async def _tables_exist(db) -> bool:
    """Return False when the migration hasn't run yet (e.g. on first boot before
    the background migration task has created the tables)."""
    from sqlalchemy import text
    row = await db.execute(text("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name   IN ('report_daily_summary', 'report_daily_device_cache',
                               'report_daily_app_usage', 'report_cache_state')
    """))
    if row.scalar() != 4:
        return False
    col = await db.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'report_daily_device_cache'
          AND column_name = 'idle_seconds'
    """))
    return col.first() is not None


async def _compute_day(db, day: date) -> None:
    """Recompute ALL three per-day tables for `day`, every company, in the
    caller's transaction (no commit here — the caller commits, so a day is
    either fully replaced or untouched).

    The day is scanned exactly twice, each half as an index-only scan on its
    partial covering index (idx_events_window_covering / idx_events_idle_covering)
    and hash-aggregated per (device[, raw app]) into small temp tables; the three
    cache tables are then derived from those, so the per-event sort that made
    COUNT(DISTINCT device_id) spill 1.4 GB never happens. Existing rows for the
    day are deleted first, so the result always equals the live definition even
    if events disappeared. The advisory lock serialises concurrent recomputes of
    the same table set (two API workers, or the fast cycle vs. a backfill).
    """
    from sqlalchemy import text
    # CAST(:day AS date) everywhere: asyncpg won't infer date params reliably.
    p = {"day": day}
    rng = ("ae.timestamp >= CAST(:day AS date) "
           "AND ae.timestamp < CAST(:day AS date) + INTERVAL '1 day'")

    await db.execute(text(f"SELECT pg_advisory_xact_lock({_DAY_LOCK_KEY})"))
    await db.execute(text("DROP TABLE IF EXISTS _rc_day_app"))
    await db.execute(text("DROP TABLE IF EXISTS _rc_day_idle"))
    await db.execute(text(f"""
        CREATE TEMP TABLE _rc_day_app ON COMMIT DROP AS
        SELECT ae.device_id, ae.app_name, SUM(ae.duration)::bigint AS secs
        FROM   activity_events ae
        WHERE  ae.event_type = 'window_change'
          AND  ae.app_name IS NOT NULL
          AND  {rng}
        GROUP  BY ae.device_id, ae.app_name
    """), p)
    await db.execute(text(f"""
        CREATE TEMP TABLE _rc_day_idle ON COMMIT DROP AS
        SELECT ae.device_id, SUM(ae.duration)::bigint AS secs
        FROM   activity_events ae
        WHERE  ae.event_type = 'idle_start'
          AND  {rng}
        GROUP  BY ae.device_id
    """), p)

    for tbl in ("report_daily_device_cache", "report_daily_summary", "report_daily_app_usage"):
        await db.execute(text(f"DELETE FROM {tbl} WHERE day = CAST(:day AS date)"), p)

    # Per device: a row exists when the device logged a window_change (real
    # app_name) or an idle_start that day — the device_count definition.
    await db.execute(text(f"""
        INSERT INTO report_daily_device_cache
            (company_id, day, device_id, active_seconds, idle_seconds, refreshed_at)
        SELECT d.company_id, CAST(:day AS date), t.device_id, t.active, t.idle, NOW()
        FROM (
            SELECT device_id, SUM(active) AS active, SUM(idle) AS idle
            FROM (
                SELECT device_id,
                       SUM(CASE WHEN app_name NOT IN {_IDLE_APPS_SQL}
                                THEN secs ELSE 0 END) AS active,
                       0 AS idle
                FROM   _rc_day_app
                GROUP  BY device_id
                UNION ALL
                SELECT device_id, 0, secs FROM _rc_day_idle
            ) u
            GROUP BY device_id
        ) t
        JOIN devices d ON d.id = t.device_id
    """), p)

    await db.execute(text("""
        INSERT INTO report_daily_summary
            (company_id, day, active_seconds, idle_seconds, device_count, refreshed_at)
        SELECT company_id, day, SUM(active_seconds), SUM(idle_seconds), COUNT(*), NOW()
        FROM   report_daily_device_cache
        WHERE  day = CAST(:day AS date)
        GROUP  BY company_id, day
    """), p)

    # Raw (device, app) groups are normalised AFTER aggregation: far fewer
    # normalize_app_name calls, and SUM is associative so the totals are exact.
    await db.execute(text(f"""
        INSERT INTO report_daily_app_usage
            (company_id, day, app_name, seconds, refreshed_at)
        SELECT d.company_id, CAST(:day AS date), normalize_app_name(a.app_name),
               SUM(a.secs), NOW()
        FROM   _rc_day_app a
        JOIN   devices d ON d.id = a.device_id
        WHERE  a.app_name NOT IN {_IDLE_APPS_SQL}
        GROUP  BY d.company_id, normalize_app_name(a.app_name)
    """), p)


async def _refresh_day_committed(day: date, *, advance_to: date | None = None) -> None:
    """One day, one transaction. `advance_to` (backfill) moves covered_since down
    to that day in the SAME transaction, and only if it is still the day right
    above it — so the horizon never claims a day that isn't committed, and two
    workers extending it concurrently stay consistent."""
    from sqlalchemy import text
    async with background_session() as db:
        await db.execute(text(f"SET LOCAL statement_timeout = '{DAY_STATEMENT_TIMEOUT_MS}'"))
        await _compute_day(db, day)
        if advance_to is not None:
            await db.execute(text("""
                UPDATE report_cache_state
                SET    covered_since = CAST(:d AS date)
                WHERE  name = :name
                  AND  covered_since = CAST(:d AS date) + 1
            """), {"d": advance_to, "name": _STATE_NAME})
        await db.commit()
    await asyncio.sleep(0)  # yield between days so ingest writes get a turn


async def _db_today() -> date:
    from sqlalchemy import text
    async with background_session() as db:
        return (await db.execute(text("SELECT CURRENT_DATE"))).scalar()


async def _fast_refresh(today: date) -> None:
    """Recompute today + yesterday (plus any days missed during downtime), then
    stamp last_fast_refresh — the one freshness signal every reader gates on
    (covers(), and home.py's summary_from_cache / device_active_from_cache).
    """
    from sqlalchemy import text

    async with background_session() as db:
        state = (await db.execute(text("""
            SELECT covered_since,
                   (last_fast_refresh AT TIME ZONE 'UTC')::date AS last_day
            FROM   report_cache_state WHERE name = :name
        """), {"name": _STATE_NAME})).first()
        summary_empty = False
        if state is None:
            summary_empty = (await db.execute(text(
                "SELECT 1 FROM report_daily_summary LIMIT 1"
            ))).first() is None

    if summary_empty:
        # Fresh / restored database: fill the 31 days home.py reads, as the
        # worker always has. Every day of it is computed in all three tables,
        # so the horizon can start that far back.
        start = today - timedelta(days=30)
        floor_day = None
    elif state is None or state.last_day is None:
        start, floor_day = today - timedelta(days=1), None
    else:
        start = min(state.last_day, today - timedelta(days=1))
        floor_day = None
        if (today - start).days > MAX_CATCHUP_DAYS:
            # Long outage: only the last MAX_CATCHUP_DAYS get recomputed, so the
            # horizon cannot honestly claim anything older than that.
            start = today - timedelta(days=MAX_CATCHUP_DAYS)
            floor_day = start

    n_days = (today - start).days + 1
    log.info("report_cache: fast refresh (%d day(s) from %s, chunked)", n_days, start)
    for i in range(n_days):
        await _refresh_day_committed(start + timedelta(days=i))

    async with background_session() as db:
        await db.execute(text("""
            INSERT INTO report_cache_state (name, covered_since, last_fast_refresh)
            VALUES (:name, CAST(:start AS date), NOW())
            ON CONFLICT (name) DO UPDATE SET
                last_fast_refresh = NOW(),
                covered_since = CASE
                    WHEN report_cache_state.covered_since IS NULL THEN CAST(:start AS date)
                    WHEN CAST(:floor AS date) IS NULL THEN report_cache_state.covered_since
                    ELSE GREATEST(report_cache_state.covered_since, CAST(:floor AS date))
                END
        """), {"name": _STATE_NAME, "start": start, "floor": floor_day})
        await db.commit()


async def _extend_horizon_one_day(db, today: date) -> date | None:
    """Compute the one day just below covered_since and lower the horizon to it.
    Runs in the caller's transaction (no commit). Returns the day added, or None
    when there is nothing to do (no state row yet, or already at COVER_DAYS)."""
    from sqlalchemy import text
    cs = (await db.execute(text(
        "SELECT covered_since FROM report_cache_state WHERE name = :name"
    ), {"name": _STATE_NAME})).scalar()
    if cs is None or cs <= today - timedelta(days=COVER_DAYS):
        return None
    d = cs - timedelta(days=1)
    await _compute_day(db, d)
    await db.execute(text("""
        UPDATE report_cache_state
        SET    covered_since = CAST(:d AS date)
        WHERE  name = :name AND covered_since = CAST(:d AS date) + 1
    """), {"d": d, "name": _STATE_NAME})
    return d


async def _backfill_step(today: date, *, max_days: int = BACKFILL_DAYS_PER_CYCLE,
                         budget_secs: float = BACKFILL_BUDGET_SECS) -> bool:
    """Extend the horizon backwards by a bounded amount of work. Returns True
    while more days remain to backfill."""
    from sqlalchemy import text
    started = time.monotonic()
    for _ in range(max_days):
        if time.monotonic() - started > budget_secs:
            break
        async with background_session() as db:
            await db.execute(text(f"SET LOCAL statement_timeout = '{DAY_STATEMENT_TIMEOUT_MS}'"))
            added = await _extend_horizon_one_day(db, today)
            await db.commit()
        if added is None:
            return False
        await asyncio.sleep(0)
    async with background_session() as db:
        cs = (await db.execute(text(
            "SELECT covered_since FROM report_cache_state WHERE name = :name"
        ), {"name": _STATE_NAME})).scalar()
    return cs is not None and cs > today - timedelta(days=COVER_DAYS)


async def _apply_retention(today: date) -> None:
    """Delete per-day rows older than RETENTION_DAYS (once per UTC day)."""
    global _last_retention_day
    if _last_retention_day == today:
        return
    from sqlalchemy import text
    async with background_session() as db:
        for tbl in ("report_daily_summary", "report_daily_device_cache", "report_daily_app_usage"):
            await db.execute(text(
                f"DELETE FROM {tbl} WHERE day < CAST(:today AS date) - CAST(:keep AS integer)"
            ), {"today": today, "keep": RETENTION_DAYS})
        await db.commit()
    _last_retention_day = today


def _plan_late_days(today: date) -> None:
    """Refill _late_pending when a trailing (hourly) or deep (daily) pass is due.
    Newest days first: recent days are the likeliest to still be receiving late
    data. A pass that was cut short by the budget keeps its remainder queued."""
    global _last_trailing_mono, _last_deep_day
    now = time.monotonic()
    due: list[date] = []
    if _last_trailing_mono is None or now - _last_trailing_mono >= TRAILING_INTERVAL:
        due += [today - timedelta(days=n) for n in range(2, TRAILING_DAYS + 1)]
        _last_trailing_mono = now
    if _last_deep_day != today:
        due += [today - timedelta(days=n) for n in range(TRAILING_DAYS + 1, DEEP_DAYS + 1)]
        _last_deep_day = today
    for d in due:
        if d not in _late_pending:
            _late_pending.append(d)


async def _late_refresh(today: date, *, budget_secs: float = LATE_BUDGET_SECS) -> bool:
    """Recompute queued settled days (late-arriving agent uploads), one day per
    transaction, staggered, for at most `budget_secs`. Days below the backfill
    horizon are skipped (the backfill computes them when it gets there). Returns
    True while queued days remain, so the loop comes back soon."""
    from sqlalchemy import text
    _plan_late_days(today)
    if not _late_pending:
        return False
    async with background_session() as db:
        covered_since = (await db.execute(text(
            "SELECT covered_since FROM report_cache_state WHERE name = :name"
        ), {"name": _STATE_NAME})).scalar()
    started = time.monotonic()
    done = 0
    while _late_pending:
        if time.monotonic() - started > budget_secs:
            break
        day = _late_pending.pop(0)
        if covered_since is None or day < covered_since:
            continue
        try:
            await _refresh_day_committed(day)
        except Exception:  # noqa: BLE001 — one bad day must not block the rest;
            log.exception("report_cache: late refresh of %s failed", day)  # next pass retries it
            continue
        done += 1
        if LATE_STAGGER_SECS:
            await asyncio.sleep(LATE_STAGGER_SECS)
    if done:
        log.info("report_cache: late-data refresh recomputed %d day(s), %d queued",
                 done, len(_late_pending))
    return bool(_late_pending)


async def _do_cycle() -> bool:
    """One worker cycle. Returns True when the loop should come back soon: the
    backfill or the late-data refresh still has work, or the tables aren't
    migrated yet."""
    global _last_fast_mono
    try:
        async with background_session() as db:
            if not await _tables_exist(db):
                log.info("report_cache: tables not ready yet — skipping refresh")
                return True
        today = await _db_today()

        if _last_fast_mono is None or time.monotonic() - _last_fast_mono >= REFRESH_INTERVAL - 1:
            await _fast_refresh(today)
            _last_fast_mono = time.monotonic()
        await _apply_retention(today)
        late_remaining = await _late_refresh(today)
        remaining = await _backfill_step(today)
        log.info("report_cache: cycle complete (backfill_remaining=%s, late_pending=%d)",
                 remaining, len(_late_pending))
        return remaining or late_remaining
    except Exception:
        log.exception("report_cache: refresh failed")
        return False


async def _loop() -> None:
    while True:
        remaining = await _do_cycle()
        await asyncio.sleep(BACKFILL_INTERVAL if remaining else REFRESH_INTERVAL)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def start() -> None:
    """Start the worker loop. Idempotent: a second call while the loop is alive
    is a no-op (a duplicate loop would double every recompute)."""
    global _worker_task, _last_fast_mono, _last_trailing_mono, _last_deep_day
    if _worker_task is not None and not _worker_task.done():
        log.info("report_cache: start() ignored — worker already running")
        return
    # A (re)started worker has no memory of when it last ran anything.
    _last_fast_mono = _last_trailing_mono = _last_deep_day = None
    _late_pending.clear()
    _worker_task = asyncio.create_task(_loop(), name="report_cache")
    log.info(
        "report_cache: worker started (fast_interval=%ds, backfill_interval=%ds, cover=%dd)",
        REFRESH_INTERVAL, BACKFILL_INTERVAL, COVER_DAYS,
    )


async def stop() -> None:
    """Stop the worker loop. Safe when never started or already stopped."""
    global _worker_task
    task, _worker_task = _worker_task, None
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — a loop that already died: nothing to stop
        log.exception("report_cache: worker had failed before stop")
    log.info("report_cache: worker stopped")


# ── Cache reads ───────────────────────────────────────────────────────────────

async def covers(db, since: date, until: date) -> bool:
    """True when the per-day tables can answer the inclusive range [since, until]:
    the state row exists, `since` is on/after the backfill horizon, and — only if
    the range touches today or yesterday — the fast cycle ran recently. Any DB
    error (tables absent) is a plain False."""
    from sqlalchemy import text
    try:
        row = (await db.execute(text("""
            SELECT covered_since,
                   COALESCE(last_fast_refresh >= NOW() - (:stale * INTERVAL '1 minute'), FALSE) AS fresh
            FROM   report_cache_state WHERE name = :name
        """), {"name": _STATE_NAME, "stale": STALE_MINUTES_DAILY})).first()
    except Exception:
        log.debug("report_cache.covers: state unreadable (table absent or error)")
        await db.rollback()
        return False
    if row is None or row.covered_since is None or since < row.covered_since:
        return False
    today = datetime.now(timezone.utc).date()
    if until >= today - timedelta(days=1) and not row.fresh:
        return False
    return True


async def _read(db, sql: str, params: dict, until: date):
    """Run a range-reader query. None on a DB error, and on an EMPTY result for a
    range that reaches today/yesterday: those days are refreshed only every
    REFRESH_INTERVAL, so "no rows yet" may just mean "not computed yet" (a brand
    new company, a device that started reporting minutes ago) and the live query
    is both cheap and exact. For settled days an empty result is a real answer
    (nothing happened) and is returned as [] so a quiet company never triggers a
    company-wide live scan."""
    from sqlalchemy import text
    try:
        rows = (await db.execute(text(sql), params)).fetchall()
    except Exception:
        log.debug("report_cache: range read failed (table absent or error)")
        await db.rollback()
        return None
    if not rows and until >= datetime.now(timezone.utc).date() - timedelta(days=1):
        return None
    return rows


async def company_summary_range(db, company_id, since: date, until: date):
    """Company-wide daily rows (day, active, idle, device_count), newest first,
    for the inclusive range, or None when the cache cannot answer it."""
    if not await covers(db, since, until):
        return None
    rows = await _read(db, SUMMARY_RANGE_SQL,
                       {"cid": str(company_id), "since": since, "until": until}, until)
    return None if rows is None else sorted(rows, key=lambda r: r.day, reverse=True)


async def device_summary_range(db, company_id, device_ids, since: date, until: date):
    """Daily rows for a set of devices (scoped users, a single device): active /
    idle summed, device_count = devices with activity that day. Newest first, or
    None when the cache cannot answer."""
    ids = [str(i) for i in device_ids]
    if not ids:
        return []
    if not await covers(db, since, until):
        return None
    rows = await _read(db, DEVICE_SUMMARY_RANGE_SQL,
                       {"cid": str(company_id), "vids": ids, "since": since, "until": until}, until)
    return None if rows is None else sorted(rows, key=lambda r: r.day, reverse=True)


async def company_top_apps_range(db, company_id, since: date, until: date, limit: int):
    """Top `limit` normalized apps company-wide over the range (app_name,
    seconds), or None when the cache cannot answer."""
    if not await covers(db, since, until):
        return None
    return await _read(db, APPS_RANGE_SQL,
                       {"cid": str(company_id), "since": since, "until": until, "limit": limit}, until)


async def _fresh_covered_since(db) -> date | None:
    """covered_since when the fast cycle ran within STALE_MINUTES_DAILY, else None
    (no state row, never refreshed, or stale). This single row is the freshness
    signal for home.py's readers — no per-row refreshed_at, so the worker never
    has to rewrite 31 days of rows just to keep them looking fresh."""
    from sqlalchemy import text
    return (await db.execute(text("""
        SELECT covered_since
        FROM   report_cache_state
        WHERE  name = :name
          AND  last_fast_refresh >= NOW() - (:stale * INTERVAL '1 minute')
    """), {"name": _STATE_NAME, "stale": STALE_MINUTES_DAILY})).scalar()


async def summary_from_cache(db, company_id: UUID, days: int):
    """Return fresh cached daily-summary rows, or None on miss / stale / table absent.

    "Fresh" = the fast cycle ran within STALE_MINUTES_DAILY; only days inside the
    covered range (day >= covered_since) are returned."""
    from sqlalchemy import text
    try:
        covered_since = await _fresh_covered_since(db)
        if covered_since is None:
            return None
        # CAST(:days AS integer) is required: with an untyped parameter asyncpg
        # lets Postgres infer `CURRENT_DATE - $n` as date-date, which then fails
        # with "operator does not exist: date >= integer" on EVERY call.
        result = await db.execute(text("""
            SELECT day, active_seconds, idle_seconds, device_count
            FROM   report_daily_summary
            WHERE  company_id = :cid
              AND  day        >= CURRENT_DATE - CAST(:days AS integer)
              AND  day        >= CAST(:covered AS date)
            ORDER BY day DESC
        """), {"cid": str(company_id), "days": days, "covered": covered_since})
        rows = result.fetchall()
        return rows if rows else None
    except Exception:
        log.debug("report_cache.summary_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def device_active_from_cache(db, company_id, since_dt, until_dt):
    """Return fresh per-device active seconds over [since, until] as
    {device_id_str: seconds}, or None on miss / stale / table absent.

    Returning an empty dict is a valid hit (no device was active). Freshness is
    the fast cycle's last_fast_refresh (STALE_MINUTES_DAILY); only days inside
    the covered range (day >= covered_since) count. A company with no summary rows in the last
    31 days (brand new) is a miss so the cheap live query decides.
    """
    from sqlalchemy import text
    try:
        covered_since = await _fresh_covered_since(db)
        if covered_since is None:
            return None
        known = (await db.execute(text("""
            SELECT 1 FROM report_daily_summary
            WHERE  company_id = :cid
              AND  day >= GREATEST(CURRENT_DATE - 31, CAST(:covered AS date))
            LIMIT  1
        """), {"cid": str(company_id), "covered": covered_since})).first()
        if known is None:
            return None

        result = await db.execute(text("""
            SELECT device_id, SUM(active_seconds) AS active
            FROM   report_daily_device_cache
            WHERE  company_id = :cid
              AND  day        BETWEEN :since AND :until
              AND  day        >= CAST(:covered AS date)
            GROUP BY device_id
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt,
               "covered": covered_since})
        return {str(r.device_id): int(r.active or 0) for r in result.fetchall()}
    except Exception:
        log.debug("report_cache.device_active_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None
