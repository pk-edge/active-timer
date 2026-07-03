"""
Tech Utilization Cache — background worker that pre-computes company-level
daily aggregates for the Tech Utilization / Website Usage / Application Usage /
Category Usage pages.

Architecture
────────────
  Request → check cache tables (fast PK / index lookup) → serve rows  (~2 ms)
      └── cache miss or filtered request → live query               (~300 ms+)

  Background worker (every 30 min), day-chunked:
    steady state — rescan only the last RECENT_DAYS (they are the only days
    that normally change), then touch refreshed_at on older rows.
    once a day  — full BACKFILL_DAYS sweep to absorb agent history backfills
    (an offline laptop can flush days-old events; those land outside the
    incremental window and would otherwise never be re-aggregated).
    first boot  — full sweep with pre-stale timestamps, flipped fresh at the
    end so a partially-built range is never served as truth.

  Tables:
    tech_daily_app_cache       (company, day, app)   seconds + user_count
    tech_daily_category_cache  (company, day, bucket) seconds + user_count
    tech_daily_totals_cache    (company, day)        headline totals
    tech_daily_url_cache       (company, day, host)  seconds + visits + users

Who benefits
────────────
Full-scope admins (scope='all') with NO group/employee filter — i.e. the
default "company overview" view most admins open first. Filtered requests
(group_id, employee_id) always run live queries since the cache stores
company-wide aggregates.

Staleness
─────────
Worker refreshes every REFRESH_INTERVAL (30 min). Rows older than
STALE_MINUTES (35 min) are ignored by read helpers → fallback to live query.
Steady-state cycles complete in seconds (day-chunked, covering-index backed),
so mixed-freshness windows are never observable in practice.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date as _date

log = logging.getLogger(__name__)

REFRESH_INTERVAL = 30 * 60  # 30 minutes between refresh cycles
STALE_MINUTES    = 35       # rows older than this are treated as a cache miss
RECENT_DAYS      = 3        # steady-state rescan window (today + 2 days back)
BACKFILL_DAYS    = 91       # full-sweep window (first boot + once daily)

_IDLE_APPS = (
    "('System Idle Process','LockApp','LogonUI','Idle','screensaver')"
)

# Host extraction must match the live query in tech_utilization.website_list.
_HOST_EXPR = (
    "REGEXP_REPLACE(LOWER(COALESCE("
    "substring(ae.url FROM '^https?://(?\\:www\\.)?([^/?#]+)'), '')), "
    "'^www\\.', '')"
)

_worker_task: asyncio.Task | None = None
_last_full_sweep: _date | None = None


# ── Refresh ───────────────────────────────────────────────────────────────────

async def _tables_exist(db) -> bool:
    from sqlalchemy import text
    row = await db.execute(text("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name   IN ('tech_daily_app_cache', 'tech_daily_url_cache')
    """))
    return row.scalar() == 2


async def _refresh_day(offset: int, *, prestale: bool) -> None:
    """Re-aggregate ONE day (CURRENT_DATE - offset) for all companies.

    Each day is its own short transaction with a fail-fast timeout, so a slow
    day can never wedge the whole cycle and ingest is never starved for long.
    `prestale` writes rows already-stale (first-boot backfill) so readers keep
    using live queries until the final flip marks the whole window fresh.
    """
    from app.db.base import AsyncSessionLocal
    from sqlalchemy import text

    ts_expr = "NOW() - INTERVAL '40 minutes'" if prestale else "NOW()"
    day_lo = "CURRENT_DATE - CAST(:offset AS integer)"
    day_hi = f"{day_lo} + INTERVAL '1 day'"
    params = {"offset": offset}

    async with AsyncSessionLocal() as db:
        # 240 s per statement: a single day is seconds in isolation, but the
        # first-boot sweep can compete with ingest + the report backfill.
        await db.execute(text("SET LOCAL statement_timeout = '240000'"))

        # ── 1. Per-app daily aggregates ────────────────────────────────
        await db.execute(text(f"""
            INSERT INTO tech_daily_app_cache
                (company_id, day, app_name, seconds, user_count, refreshed_at)
            SELECT
                d.company_id,
                DATE(ae.timestamp)              AS day,
                normalize_app_name(ae.app_name) AS app_name,
                SUM(ae.duration)                AS seconds,
                COUNT(DISTINCT d.employee_id)   AS user_count,
                {ts_expr}                       AS refreshed_at
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= {day_lo}
              AND ae.timestamp <  {day_hi}
            GROUP BY d.company_id, DATE(ae.timestamp), normalize_app_name(ae.app_name)
            ON CONFLICT (company_id, day, app_name) DO UPDATE SET
                seconds      = EXCLUDED.seconds,
                user_count   = EXCLUDED.user_count,
                refreshed_at = EXCLUDED.refreshed_at
        """), params)

        # ── 2a. Per-category daily aggregates — named categories ───────
        await db.execute(text(f"""
            INSERT INTO tech_daily_category_cache
                (company_id, day, bucket_key, category_id, category_name, color,
                 seconds, user_count, refreshed_at)
            SELECT
                d.company_id,
                DATE(ae.timestamp)              AS day,
                c.id::text                      AS bucket_key,
                c.id                            AS category_id,
                c.name                          AS category_name,
                c.color,
                SUM(ae.duration)                AS seconds,
                COUNT(DISTINCT d.employee_id)   AS user_count,
                {ts_expr}                       AS refreshed_at
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            JOIN app_category_rules r
                 ON r.app_pattern = normalize_app_name(ae.app_name)
                AND r.company_id  = d.company_id
            JOIN app_categories c
                 ON c.id = r.category_id
                AND c.is_deleted = FALSE
            WHERE ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= {day_lo}
              AND ae.timestamp <  {day_hi}
            GROUP BY d.company_id, DATE(ae.timestamp), c.id, c.name, c.color
            ON CONFLICT (company_id, day, bucket_key) DO UPDATE SET
                category_id   = EXCLUDED.category_id,
                category_name = EXCLUDED.category_name,
                color         = EXCLUDED.color,
                seconds       = EXCLUDED.seconds,
                user_count    = EXCLUDED.user_count,
                refreshed_at  = EXCLUDED.refreshed_at
        """), params)

        # ── 2b. Per-category daily aggregates — uncategorized bucket ──
        await db.execute(text(f"""
            INSERT INTO tech_daily_category_cache
                (company_id, day, bucket_key, category_id, category_name, color,
                 seconds, user_count, refreshed_at)
            SELECT
                d.company_id,
                DATE(ae.timestamp)            AS day,
                'uncategorized'               AS bucket_key,
                NULL                          AS category_id,
                'Uncategorized'               AS category_name,
                '#94a3b8'                     AS color,
                SUM(ae.duration)              AS seconds,
                COUNT(DISTINCT d.employee_id) AS user_count,
                {ts_expr}                     AS refreshed_at
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= {day_lo}
              AND ae.timestamp <  {day_hi}
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r2
                  WHERE r2.company_id  = d.company_id
                    AND r2.app_pattern = normalize_app_name(ae.app_name)
              )
            GROUP BY d.company_id, DATE(ae.timestamp)
            ON CONFLICT (company_id, day, bucket_key) DO UPDATE SET
                seconds      = EXCLUDED.seconds,
                user_count   = EXCLUDED.user_count,
                refreshed_at = EXCLUDED.refreshed_at
        """), params)

        # ── 3. Company-day headline totals ────────────────────────────
        await db.execute(text(f"""
            INSERT INTO tech_daily_totals_cache
                (company_id, day, total_seconds, total_apps, total_users, refreshed_at)
            SELECT
                d.company_id,
                DATE(ae.timestamp)                              AS day,
                SUM(ae.duration)                                AS total_seconds,
                COUNT(DISTINCT normalize_app_name(ae.app_name)) AS total_apps,
                COUNT(DISTINCT d.employee_id)                   AS total_users,
                {ts_expr}                                       AS refreshed_at
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= {day_lo}
              AND ae.timestamp <  {day_hi}
            GROUP BY d.company_id, DATE(ae.timestamp)
            ON CONFLICT (company_id, day) DO UPDATE SET
                total_seconds = EXCLUDED.total_seconds,
                total_apps    = EXCLUDED.total_apps,
                total_users   = EXCLUDED.total_users,
                refreshed_at  = EXCLUDED.refreshed_at
        """), params)

        # ── 4. Per-host daily URL aggregates ──────────────────────────
        # Duration uses the same LEAD() session-gap estimate as the live
        # website_list query (min 30s, cap 600s, 3-min default for the last
        # visit). LEAD is computed within the day, so a session spanning
        # midnight gets the 3-min default at the boundary — negligible.
        await db.execute(text(f"""
            INSERT INTO tech_daily_url_cache
                (company_id, day, host, seconds, visits, user_count, refreshed_at)
            SELECT company_id, day, host,
                   SUM(secs)                    AS seconds,
                   COUNT(*)                     AS visits,
                   COUNT(DISTINCT employee_id)  AS user_count,
                   {ts_expr}                    AS refreshed_at
            FROM (
                SELECT
                    d.company_id,
                    DATE(ae.timestamp) AS day,
                    {_HOST_EXPR}       AS host,
                    d.employee_id,
                    GREATEST(LEAST(
                        EXTRACT(EPOCH FROM (
                            COALESCE(
                                LEAD(ae.timestamp) OVER (PARTITION BY ae.device_id ORDER BY ae.timestamp),
                                ae.timestamp + interval '3 minutes'
                            ) - ae.timestamp
                        ))::int, 600
                    ), 30) AS secs
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                WHERE ae.event_type = 'url_visit'
                  AND ae.url IS NOT NULL
                  AND ae.timestamp >= {day_lo}
                  AND ae.timestamp <  {day_hi}
            ) x
            WHERE host <> ''
            GROUP BY company_id, day, host
            ON CONFLICT (company_id, day, host) DO UPDATE SET
                seconds      = EXCLUDED.seconds,
                visits       = EXCLUDED.visits,
                user_count   = EXCLUDED.user_count,
                refreshed_at = EXCLUDED.refreshed_at
        """), params)

        await db.commit()


async def _touch_window(days: int) -> None:
    """Mark the whole window fresh in one shot per table (no event rescans)."""
    from app.db.base import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        for tbl in ("tech_daily_app_cache", "tech_daily_category_cache",
                    "tech_daily_totals_cache", "tech_daily_url_cache"):
            await db.execute(text(f"""
                UPDATE {tbl}
                SET    refreshed_at = NOW()
                WHERE  day >= CURRENT_DATE - CAST(:days AS integer)
            """), {"days": days})
        await db.commit()


async def _do_refresh() -> None:
    global _last_full_sweep
    from app.db.base import AsyncSessionLocal
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as db:
            if not await _tables_exist(db):
                log.info("tech_cache: tables not ready yet — skipping refresh")
                return
            has_row = await db.execute(text("SELECT 1 FROM tech_daily_app_cache LIMIT 1"))
            is_full = has_row.fetchone() is None

        today = _date.today()
        run_full = is_full or _last_full_sweep != today
        days_back = BACKFILL_DAYS if run_full else RECENT_DAYS

        log.info("tech_cache: refresh start (days=%d, full=%s)", days_back, run_full)
        for offset in range(days_back):
            # First-boot rows are written pre-stale so a partially built range
            # is never served; the touch below flips the window fresh at once.
            # A failed day aborts the sweep WITHOUT the touch-flip, so readers
            # keep falling back to live queries — never a half-built range.
            await _refresh_day(offset, prestale=is_full)
            await asyncio.sleep(0.25)  # let ingest writes through between days

        await _touch_window(BACKFILL_DAYS)
        if run_full:
            _last_full_sweep = today
        log.info("tech_cache: refresh complete (days=%d)", days_back)
    except Exception:
        log.exception("tech_cache: refresh failed")


async def _loop() -> None:
    # Stagger behind report_cache's boot refresh so the two workers' first
    # sweeps don't compete for I/O (they share the same events table).
    await asyncio.sleep(90)
    await _do_refresh()           # warm cache shortly after boot
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        await _do_refresh()


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def start() -> None:
    global _worker_task
    _worker_task = asyncio.create_task(_loop(), name="tech_cache")
    log.info("tech_cache: worker started (interval=%ds)", REFRESH_INTERVAL)


async def stop() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    log.info("tech_cache: worker stopped")


# ── Cache reads ───────────────────────────────────────────────────────────────

_STALE_PARAM = {"stale": STALE_MINUTES}


async def summary_from_cache(db, company_id, since_dt, until_dt):
    """Return (total_seconds, total_apps, total_users, top_cat_row, top_app_row)
    or None on cache miss / stale / table absent."""
    from sqlalchemy import text
    try:
        totals = (await db.execute(text("""
            SELECT
                COALESCE(SUM(seconds), 0)      AS total_seconds,
                COUNT(DISTINCT app_name)        AS total_apps
            FROM tech_daily_app_cache
            WHERE company_id   = :cid
              AND day           BETWEEN :since AND :until
              AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt, **_STALE_PARAM}
        )).fetchone()

        if totals is None or totals.total_apps == 0:
            return None

        users = (await db.execute(text("""
            SELECT COALESCE(MAX(total_users), 0) AS total_users
            FROM tech_daily_totals_cache
            WHERE company_id   = :cid
              AND day           BETWEEN :since AND :until
              AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt, **_STALE_PARAM}
        )).fetchone()

        top_cat = (await db.execute(text("""
            SELECT category_name, color, SUM(seconds) AS seconds
            FROM tech_daily_category_cache
            WHERE company_id   = :cid
              AND day           BETWEEN :since AND :until
              AND bucket_key   != 'uncategorized'
              AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
            GROUP BY category_name, color
            ORDER BY seconds DESC
            LIMIT 1
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt, **_STALE_PARAM}
        )).fetchone()

        top_app = (await db.execute(text("""
            SELECT app_name, SUM(seconds) AS seconds
            FROM tech_daily_app_cache
            WHERE company_id   = :cid
              AND day           BETWEEN :since AND :until
              AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
            GROUP BY app_name
            ORDER BY seconds DESC
            LIMIT 1
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt, **_STALE_PARAM}
        )).fetchone()

        return totals, users, top_cat, top_app
    except Exception:
        log.debug("tech_cache.summary_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def category_allocation_from_cache(db, company_id, since_dt, until_dt,
                                          prev_since_dt, prev_until_dt):
    """Return (current_rows, prev_rows) from cache, or None on miss/stale/absent."""
    from sqlalchemy import text
    try:
        # category_id is cast to text — the response schema expects a string and
        # a raw UUID here 500s the endpoint at validation time.
        current = (await db.execute(text("""
            WITH period AS (
                SELECT bucket_key, category_id::text AS category_id,
                       category_name, color,
                       SUM(seconds) AS seconds, SUM(user_count) AS users
                FROM tech_daily_category_cache
                WHERE company_id   = :cid
                  AND day           BETWEEN :since AND :until
                  AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
                GROUP BY bucket_key, category_id::text, category_name, color
            ),
            total AS (SELECT NULLIF(SUM(seconds), 0) AS t FROM period)
            SELECT p.*, ROUND(100.0 * p.seconds / t.t, 1) AS pct
            FROM period p, total t
            ORDER BY p.seconds DESC
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt, **_STALE_PARAM}
        )).fetchall()

        if not current:
            return None

        prev = (await db.execute(text("""
            WITH period AS (
                SELECT category_name,
                       SUM(seconds) AS seconds, SUM(user_count) AS users
                FROM tech_daily_category_cache
                WHERE company_id   = :cid
                  AND day           BETWEEN :prev_since AND :prev_until
                  AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
                GROUP BY category_name
            ),
            total AS (SELECT NULLIF(SUM(seconds), 0) AS t FROM period)
            SELECT p.category_name, p.seconds, p.users,
                   ROUND(100.0 * p.seconds / t.t, 1) AS pct
            FROM period p, total t
        """), {"cid": str(company_id), "prev_since": prev_since_dt, "prev_until": prev_until_dt,
               **_STALE_PARAM}
        )).fetchall()

        return current, {r.category_name: r for r in prev}
    except Exception:
        log.debug("tech_cache.category_allocation_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def app_list_from_cache(db, company_id, since_dt, until_dt, limit: int):
    """Return app rows with category info from cache, or None on miss/stale/absent."""
    from sqlalchemy import text
    try:
        rows = (await db.execute(text("""
            WITH period_apps AS (
                SELECT app_name,
                       SUM(seconds)    AS seconds,
                       SUM(user_count) AS user_count
                FROM tech_daily_app_cache
                WHERE company_id   = :cid
                  AND day           BETWEEN :since AND :until
                  AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
                GROUP BY app_name
            ),
            total AS (SELECT NULLIF(SUM(seconds), 0) AS t FROM period_apps)
            SELECT
                a.app_name,
                COALESCE(c.id::text, NULL)        AS category_id,
                COALESCE(c.name, 'Uncategorized') AS category_name,
                COALESCE(c.color, '#94a3b8')       AS color,
                a.seconds,
                a.user_count,
                ROUND(100.0 * a.seconds / t.t, 2) AS pct
            FROM period_apps a
            LEFT JOIN app_category_rules r
                   ON r.app_pattern = a.app_name AND r.company_id = :cid
            LEFT JOIN app_categories c
                   ON c.id = r.category_id AND c.is_deleted = FALSE
            CROSS JOIN total t
            ORDER BY a.seconds DESC
            LIMIT :limit
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt,
               "limit": limit, **_STALE_PARAM}
        )).fetchall()

        return rows if rows else None
    except Exception:
        log.debug("tech_cache.app_list_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def app_usage_from_cache(db, company_id, since_dt, until_dt, limit: int):
    """Rows (app_name, total_secs, users, pct) for /tech/apps, or None."""
    from sqlalchemy import text
    try:
        rows = (await db.execute(text("""
            WITH agg AS (
                SELECT app_name,
                       SUM(seconds)    AS total_secs,
                       SUM(user_count) AS users
                FROM tech_daily_app_cache
                WHERE company_id   = :cid
                  AND day           BETWEEN :since AND :until
                  AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
                GROUP BY app_name
            ),
            tot AS (SELECT NULLIF(SUM(total_secs), 0) AS t FROM agg)
            SELECT a.app_name, a.total_secs, a.users,
                   ROUND(100.0 * a.total_secs / t.t, 1) AS pct
            FROM agg a, tot t
            ORDER BY a.total_secs DESC
            LIMIT :limit
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt,
               "limit": limit, **_STALE_PARAM}
        )).fetchall()
        return rows if rows else None
    except Exception:
        log.debug("tech_cache.app_usage_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def category_usage_from_cache(db, company_id, since_dt, until_dt):
    """Rows (cat_id, name, color, total_secs, users, pct) for /tech/category-usage."""
    from sqlalchemy import text
    try:
        rows = (await db.execute(text("""
            WITH agg AS (
                SELECT category_id::text AS cat_id,
                       category_name     AS name,
                       color,
                       SUM(seconds)      AS total_secs,
                       SUM(user_count)   AS users
                FROM tech_daily_category_cache
                WHERE company_id   = :cid
                  AND day           BETWEEN :since AND :until
                  AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
                GROUP BY category_id::text, category_name, color
            ),
            tot AS (SELECT NULLIF(SUM(total_secs), 0) AS t FROM agg)
            SELECT a.cat_id, a.name, a.color, a.total_secs, a.users,
                   ROUND(100.0 * a.total_secs / t.t, 1) AS pct
            FROM agg a, tot t
            ORDER BY a.total_secs DESC
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt, **_STALE_PARAM}
        )).fetchall()
        return rows if rows else None
    except Exception:
        log.debug("tech_cache.category_usage_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def website_list_from_cache(db, company_id, since_dt, until_dt, limit: int):
    """Rows (host, total_secs, visits, users, pct) for /tech/websites, or None."""
    from sqlalchemy import text
    try:
        rows = (await db.execute(text("""
            WITH agg AS (
                SELECT host,
                       SUM(seconds)    AS total_secs,
                       SUM(visits)     AS visits,
                       MAX(user_count) AS users
                FROM tech_daily_url_cache
                WHERE company_id   = :cid
                  AND day           BETWEEN :since AND :until
                  AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
                GROUP BY host
            ),
            tot AS (SELECT NULLIF(SUM(total_secs), 0) AS t FROM agg)
            SELECT a.host, a.total_secs, a.visits, a.users,
                   ROUND(100.0 * a.total_secs / t.t, 1) AS pct
            FROM agg a, tot t
            ORDER BY a.total_secs DESC
            LIMIT :limit
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt,
               "limit": limit, **_STALE_PARAM}
        )).fetchall()
        return rows if rows else None
    except Exception:
        log.debug("tech_cache.website_list_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None


async def uncategorized_from_cache(db, company_id, since_dt, until_dt, limit: int):
    """Return (app_rows, url_rows) with no category rule yet, or None.

    Rules may be stored with or without a leading 'www.' for hosts, so the
    anti-join checks both forms.
    """
    from sqlalchemy import text
    try:
        apps = (await db.execute(text("""
            SELECT app_name        AS pattern,
                   SUM(seconds)    AS seconds,
                   MAX(user_count) AS user_count,
                   MAX(day)        AS last_seen
            FROM tech_daily_app_cache a
            WHERE company_id   = :cid
              AND day           BETWEEN :since AND :until
              AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r
                  WHERE r.company_id = :cid AND r.app_pattern = a.app_name
              )
            GROUP BY app_name
            ORDER BY seconds DESC
            LIMIT :limit
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt,
               "limit": limit, **_STALE_PARAM}
        )).fetchall()

        urls = (await db.execute(text("""
            SELECT host            AS pattern,
                   SUM(visits)     AS seconds,
                   MAX(user_count) AS user_count,
                   MAX(day)        AS last_seen
            FROM tech_daily_url_cache u
            WHERE company_id   = :cid
              AND day           BETWEEN :since AND :until
              AND refreshed_at >= NOW() - (:stale * INTERVAL '1 minute')
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r
                  WHERE r.company_id = :cid
                    AND (r.app_pattern = u.host OR r.app_pattern = 'www.' || u.host)
              )
            GROUP BY host
            ORDER BY seconds DESC
            LIMIT :limit
        """), {"cid": str(company_id), "since": since_dt, "until": until_dt,
               "limit": limit, **_STALE_PARAM}
        )).fetchall()

        if not apps and not urls:
            return None
        return apps, urls
    except Exception:
        log.debug("tech_cache.uncategorized_from_cache: cache miss (table absent or error)")
        await db.rollback()
        return None
