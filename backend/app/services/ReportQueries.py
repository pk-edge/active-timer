"""Single home for the device_full and fleet report SQL.

The same queries used to be copy-pasted into the background workers, the
Redis-down synchronous fallback in POST /jobs, and the on-screen GET endpoints.
They now all call build_device_full_report / build_fleet_report so the numbers
an export shows are, by construction, the numbers the page shows.

Session handling: pass `db` to run the queries sequentially on that one session
(request-scoped callers; an AsyncSession cannot run concurrent statements), or
leave it None to run them concurrently, each on its own BackgroundSessionLocal
session (background workers, which have no request session — the isolated
background pool, so exports never take HTTP-request connections).

Statement budget: request-path computes (db given) run under
LIVE_STATEMENT_TIMEOUT_MS (60 s) like every other live endpoint, so a request
can never pin an HTTP pool connection for minutes (and the single-flight lock,
90 s, always outlives the statement). Only background jobs (db=None) get
BACKGROUND_STATEMENT_TIMEOUT_MS (300 s) for multi-year scans.

Scoping is the caller's job: resolve visible_device_ids() while the request
session and `admin` are alive and hand the result in as `device_ids`
(None = every device in the company, list = only those; [] = nothing).
"""
import asyncio
import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import text

from app.core import report_cache
from app.core.report_cache import _IDLE_APPS_SQL
from app.core.config import get_settings

# Active-time CASE expression (window_change only, real apps). OS idle/lock-
# screen processes show up as foreground-window events when the screen is idle
# or locked; they never count as work. The list is the report cache worker's
# (shared with the home dashboard, macOS loginwindow/ScreenSaverEngine
# included), so cached, live and exported totals agree.

_ACTIVE_CASE = f"""CASE
    WHEN ae.event_type = 'window_change'
     AND ae.app_name IS NOT NULL
     AND ae.app_name NOT IN {_IDLE_APPS_SQL}
    THEN ae.duration ELSE 0
END"""
# SQL fragment to exclude idle app names from top-app lists
_EXCL_IDLE = f"AND ae.app_name NOT IN {_IDLE_APPS_SQL}"

ONLINE_CUTOFF_SECS = 120  # must match devices endpoint

# Half-open [since, until + 1 day) — sargable on (device_id, timestamp).
_RANGE = (
    "ae.timestamp >= CAST(:since AS date) "
    "AND ae.timestamp < CAST(:until AS date) + INTERVAL '1 day'"
)

ProgressFn = Callable[[int], Awaitable[None]]


# Per-statement budgets. The engine's session-level statement_timeout
# (DB_STATEMENT_TIMEOUT_MS, 120 s) is sized for request traffic.
#  * LIVE: anything computed while a client waits on an HTTP-pool connection.
#    Must stay below reports._LIVE_LOCK_TTL (the single-flight lock).
#  * BACKGROUND: export jobs (db=None) on the background pool, where a
#    multi-year activity_events scan can legitimately run longer.
LIVE_STATEMENT_TIMEOUT_MS = 60_000
BACKGROUND_STATEMENT_TIMEOUT_MS = 300_000


def _timeout_sql(ms: int) -> str:
    return f"SET LOCAL statement_timeout = '{int(ms)}'"


# Caps how many report queries hold a background-pool connection at once, across
# every concurrent job in this process, and never exceeds the pool itself
# (BACKGROUND_DB_POOL_SIZE + BACKGROUND_DB_MAX_OVERFLOW, default 2): without the
# cap overlapping exports would queue on the pool and hit its checkout timeout.
_s = get_settings()
_REPORT_DB_SLOTS = asyncio.Semaphore(
    max(1, min(2, _s.BACKGROUND_DB_POOL_SIZE + _s.BACKGROUND_DB_MAX_OVERFLOW)))

Query = tuple[str, dict]  # (sql, params)


async def _fetch(db, sql: str, params: dict, timeout_ms: int = BACKGROUND_STATEMENT_TIMEOUT_MS) -> list:
    """Run one statement and return all rows — on `db` if given, else on a
    private short-lived background-pool session so several _fetch calls can run
    in parallel."""
    if db is not None:
        return (await db.execute(text(sql), params)).fetchall()
    async with _REPORT_DB_SLOTS:
        async with report_cache.background_session() as own:
            await own.execute(text(_timeout_sql(timeout_ms)))
            return (await own.execute(text(sql), params)).fetchall()


async def _run_all(db, *queries: Query, timeout_ms: Optional[int] = None) -> list:
    """Run (sql, params) queries. Concurrent when each owns its session
    (db is None), otherwise sequential on the shared session. Coroutines are
    created only when they are about to run, so a failure leaves none unawaited;
    on the concurrent path the first failure cancels the sibling queries.
    `timeout_ms` defaults to LIVE (60 s) on a request session and BACKGROUND
    (300 s) on private sessions."""
    if db is not None:
        # lasts for this transaction
        await db.execute(text(_timeout_sql(timeout_ms or LIVE_STATEMENT_TIMEOUT_MS)))
        return [await _fetch(db, sql, params) for sql, params in queries]

    t_ms = timeout_ms or BACKGROUND_STATEMENT_TIMEOUT_MS
    tasks = [asyncio.ensure_future(_fetch(None, sql, params, t_ms)) for sql, params in queries]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _progress(cb: Optional[ProgressFn], pct: int) -> None:
    if cb:
        await cb(pct)


# ── Live aggregation SQL (the fallback when the per-day cache can't answer) ───
# One definition for the on-screen endpoints and the exports. Each query is split
# into its window_change half and its idle_start half so each can run as an
# index-only scan on its partial covering index (idx_events_window_covering /
# idx_events_idle_covering, or the device-led variant when scoped), and is
# pre-aggregated per (day, device) BEFORE rolling up. Counting devices with
# COUNT(DISTINCT device_id) straight over the events instead sorts tens of
# millions of rows to disk. Params: :cid, :since, :until, :vids (scoped only),
# :limit (top apps). Scope is applied inside the scans, the company filter after
# pre-aggregation on the small per-device rows.
#
# device_count = devices that logged a window_change (real app_name) or an
# idle_start that day — the same definition the cache tables store.

def daily_summary_sql(*, scoped: bool) -> str:
    ae_scope = " AND ae.device_id = ANY(:vids)" if scoped else ""
    return f"""
        SELECT u.day,
               SUM(u.active) AS active,
               SUM(u.idle)   AS idle,
               COUNT(DISTINCT u.device_id) AS device_count
        FROM (
            SELECT DATE(ae.timestamp) AS day, ae.device_id,
                   SUM(CASE WHEN ae.app_name NOT IN {_IDLE_APPS_SQL}
                            THEN ae.duration ELSE 0 END) AS active,
                   0 AS idle
            FROM   activity_events ae
            WHERE  ae.event_type = 'window_change'
              AND  ae.app_name IS NOT NULL
              AND  {_RANGE}{ae_scope}
            GROUP  BY DATE(ae.timestamp), ae.device_id
            UNION ALL
            SELECT DATE(ae.timestamp) AS day, ae.device_id,
                   0 AS active,
                   SUM(ae.duration) AS idle
            FROM   activity_events ae
            WHERE  ae.event_type = 'idle_start'
              AND  {_RANGE}{ae_scope}
            GROUP  BY DATE(ae.timestamp), ae.device_id
        ) u
        JOIN devices d ON d.id = u.device_id
        WHERE d.company_id = :cid
        GROUP BY u.day
    """


def top_apps_sql(*, scoped: bool) -> str:
    ae_scope = " AND ae.device_id = ANY(:vids)" if scoped else ""
    return f"""
        SELECT normalize_app_name(a.app_name) AS app_name,
               SUM(a.secs) AS seconds
        FROM (
            SELECT ae.device_id, ae.app_name, SUM(ae.duration) AS secs
            FROM   activity_events ae
            WHERE  ae.event_type = 'window_change'
              AND  ae.app_name IS NOT NULL
              AND  {_RANGE}{ae_scope}
              {_EXCL_IDLE}
            GROUP  BY ae.device_id, ae.app_name
        ) a
        JOIN devices d ON d.id = a.device_id
        WHERE d.company_id = :cid
        GROUP BY normalize_app_name(a.app_name)
        ORDER BY seconds DESC, app_name
        LIMIT :limit
    """


def top_devices_sql(*, scoped: bool, from_cache: bool) -> str:
    """Ten most active devices (hostname + assigned employee), active > 0."""
    if from_cache:
        vids = " AND device_id = ANY(:vids)" if scoped else ""
        inner = f"""
            SELECT device_id, SUM(active_seconds) AS active
            FROM   report_daily_device_cache
            WHERE  company_id = :cid
              AND  day BETWEEN CAST(:since AS date) AND CAST(:until AS date){vids}
            GROUP  BY device_id
            HAVING SUM(active_seconds) > 0
        """
    else:
        ae_scope = " AND ae.device_id = ANY(:vids)" if scoped else ""
        inner = f"""
            SELECT ae.device_id, SUM(ae.duration) AS active
            FROM   activity_events ae
            WHERE  ae.event_type = 'window_change'
              AND  ae.app_name IS NOT NULL
              AND  {_RANGE}{ae_scope}
              {_EXCL_IDLE}
            GROUP  BY ae.device_id
            HAVING SUM(ae.duration) > 0
        """
    return f"""
        SELECT d.id::text AS device_id,
               d.hostname,
               e.name     AS employee_name,
               a.active   AS active_seconds
        FROM ({inner}) a
        JOIN   devices d ON d.id = a.device_id
        LEFT JOIN employees e ON e.id = d.employee_id
        WHERE  d.company_id = :cid
        ORDER  BY a.active DESC, d.id
        LIMIT  10
    """


async def live_daily_summary(db, company_id: str, since: date, until: date,
                             device_ids: Optional[list[str]]) -> list:
    """Live (day, active, idle, device_count) rows, unsorted. `device_ids`:
    None = whole company, list = only those devices."""
    params: dict = {"cid": company_id, "since": since, "until": until}
    if device_ids is not None:
        params["vids"] = list(device_ids)
    return (await db.execute(
        text(daily_summary_sql(scoped=device_ids is not None)), params)).fetchall()


async def live_top_apps(db, company_id: str, since: date, until: date,
                        device_ids: Optional[list[str]], limit: int) -> list:
    params: dict = {"cid": company_id, "since": since, "until": until, "limit": limit}
    if device_ids is not None:
        params["vids"] = list(device_ids)
    return (await db.execute(
        text(top_apps_sql(scoped=device_ids is not None)), params)).fetchall()


async def _cache_covers(db, since: date, until: date) -> bool:
    """Can the per-day cache tables answer [since, until]? Checked on `db` when
    given, else on a short-lived private session. Never raises."""
    try:
        if db is not None:
            return await report_cache.covers(db, since, until)
        async with _REPORT_DB_SLOTS:
            async with report_cache.background_session() as own:
                return await report_cache.covers(own, since, until)
    except Exception:  # noqa: BLE001
        return False


def _cache_answer_unsure(until: date) -> bool:
    """True when a range reaches today/yesterday — days the worker refreshes only
    every few minutes, so an EMPTY cache answer may just mean "not computed yet"
    and the exact live query should decide (same rule as report_cache._read)."""
    return until >= datetime.now(timezone.utc).date() - timedelta(days=1)


# ── Scope fingerprint / cache key ─────────────────────────────────────────────

def scope_fingerprint(device_ids: Optional[list[str]]) -> str:
    """Stable id for a visibility scope: "all", or a digest of the sorted id set.

    Two callers with the same visible devices share a cache entry; any
    difference in scope (including empty vs all) yields a different key."""
    if device_ids is None:
        return "all"
    digest = hashlib.sha1(",".join(sorted(str(i) for i in device_ids)).encode()).hexdigest()
    return f"scoped:{digest[:16]}"


def fleet_cache_key(company_id: str, since: date, until: date,
                    device_ids: Optional[list[str]]) -> tuple:
    return ("report:fleet", str(company_id), scope_fingerprint(device_ids), str(since), str(until))


# ── device_full ───────────────────────────────────────────────────────────────

async def build_device_full_report(
    company_id: str, device_id: str, since: date, until: date,
    *, db=None, on_progress: Optional[ProgressFn] = None,
    timeout_ms: Optional[int] = None,
) -> Optional[dict]:
    """DeviceFullReportOut-shaped dict for one device, or None if the device
    doesn't exist in the company. The caller must have already checked the
    device is visible to the requester. `timeout_ms`: per-statement budget,
    default LIVE (60 s) with `db`, BACKGROUND (300 s) without."""
    await _progress(on_progress, 5)
    params = {"did": device_id, "since": since, "until": until}

    dev_rows = await _fetch(db, """
        SELECT d.hostname, d.platform, d.enrolled_at, d.last_seen,
               e.name AS employee_name
        FROM   devices d
        LEFT JOIN employees e ON e.id = d.employee_id
        WHERE  d.id = :did AND d.company_id = :cid AND d.is_deleted = FALSE
    """, {"did": device_id, "cid": company_id})
    if not dev_rows:
        return None
    dev_row = dev_rows[0]
    await _progress(on_progress, 20)

    # The daily rows come from the per-day device cache when it covers the range
    # (same numbers the live query produces, from a few hundred rows), else live.
    d_params = {"cid": company_id, "vids": [device_id], "since": since, "until": until}
    live_daily_q: Query = (daily_summary_sql(scoped=True), d_params)
    from_cache = await _cache_covers(db, since, until)
    daily_q: Query = (report_cache.DEVICE_SUMMARY_RANGE_SQL, d_params) if from_cache else live_daily_q

    daily, apps_raw, top_urls = await _run_all(
        db,
        daily_q,
        (f"""
            SELECT normalize_app_name(ae.app_name) AS app_name,
                   SUM(ae.duration) AS seconds
            FROM   activity_events ae
            WHERE  ae.device_id = :did
              AND  ae.event_type = 'window_change'
              AND  ae.app_name IS NOT NULL
              AND  {_RANGE}
              {_EXCL_IDLE}
            GROUP  BY normalize_app_name(ae.app_name)
            ORDER  BY seconds DESC LIMIT 20
        """, params),
        (f"""
            SELECT substring(ae.url FROM '^https?://([^/]+)') AS host,
                   COUNT(*) AS visits
            FROM   activity_events ae
            WHERE  ae.device_id = :did
              AND  ae.event_type = 'url_visit'
              AND  ae.url IS NOT NULL
              AND  {_RANGE}
            GROUP  BY host
            HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
            ORDER  BY visits DESC LIMIT 15
        """, params),
        timeout_ms=timeout_ms,
    )
    await _progress(on_progress, 90)

    if from_cache and not daily and _cache_answer_unsure(until):
        daily = (await _run_all(db, live_daily_q, timeout_ms=timeout_ms))[0]
    daily = sorted(daily, key=lambda r: r.day)
    total_active   = sum(int(r.active or 0) for r in daily)
    total_idle     = sum(int(r.idle   or 0) for r in daily)
    working_days   = len([r for r in daily if int(r.active or 0) > 0])
    total_app_secs = sum(int(r.seconds or 0) for r in apps_raw) or 1

    return {
        "device": {
            "id": device_id,
            "hostname": dev_row.hostname,
            "platform": (dev_row.platform or "").capitalize(),
            "employee_name": dev_row.employee_name,
            "enrolled_at": dev_row.enrolled_at.strftime("%Y-%m-%d") if dev_row.enrolled_at else "",
            "last_seen": dev_row.last_seen.strftime("%Y-%m-%d %H:%M UTC") if dev_row.last_seen else None,
        },
        "since": str(since),
        "until": str(until),
        "total_active_seconds": total_active,
        "total_idle_seconds": total_idle,
        "working_days": working_days,
        "daily_breakdown": [
            {"date": str(r.day), "active_seconds": int(r.active or 0), "idle_seconds": int(r.idle or 0)}
            for r in daily
        ],
        "top_apps": [
            {
                "app_name": r.app_name,
                "seconds": int(r.seconds or 0),
                "pct": round(int(r.seconds or 0) / total_app_secs * 100, 1),
            }
            for r in apps_raw
        ],
        "top_urls": [{"url": r.host, "visits": int(r.visits or 0)} for r in top_urls],
    }


# ── fleet ─────────────────────────────────────────────────────────────────────

async def build_fleet_report(
    company_id: str, since: date, until: date, device_ids: Optional[list[str]],
    *, db=None, on_progress: Optional[ProgressFn] = None,
    timeout_ms: Optional[int] = None,
) -> dict:
    """FleetReportOut-shaped dict restricted to `device_ids` (None = whole
    company). `timeout_ms` as in build_device_full_report. Every query — device counts, platform split, daily activity, top
    apps, top devices — carries the same scope, so a scoped caller can neither
    see nor infer devices outside it."""
    await _progress(on_progress, 5)

    # Same binding as the roster / _scope_clause: plain list[str] against a uuid column.
    dev_scope = "" if device_ids is None else " AND id = ANY(:vids)"
    vids = {} if device_ids is None else {"vids": list(device_ids)}

    p_meta = {"cid": company_id, "cutoff": ONLINE_CUTOFF_SECS, **vids}
    p_plat = {"cid": company_id, **vids}
    p_time = {"cid": company_id, "since": since, "until": until, "limit": 20, **vids}
    scoped = device_ids is not None

    # Daily activity, top apps and top devices come from the per-day cache tables
    # when they cover the range (company-wide: summary + app tables, scoped:
    # the per-device table filtered to the caller's devices), else from the live
    # split-and-pre-aggregate queries. Scoped top apps always run live (a few
    # devices, covering index). Same numbers either way.
    def _time_queries(from_cache: bool) -> tuple[Query, Query, Query]:
        if not from_cache:
            return ((daily_summary_sql(scoped=scoped), p_time),
                    (top_apps_sql(scoped=scoped), p_time),
                    (top_devices_sql(scoped=scoped, from_cache=False), p_time))
        return ((report_cache.DEVICE_SUMMARY_RANGE_SQL if scoped else report_cache.SUMMARY_RANGE_SQL, p_time),
                ((top_apps_sql(scoped=True), p_time) if scoped else (report_cache.APPS_RANGE_SQL, p_time)),
                (top_devices_sql(scoped=scoped, from_cache=True), p_time))

    from_cache = await _cache_covers(db, since, until)
    daily_q, apps_q, devices_q = _time_queries(from_cache)

    counts_rows, platforms, daily, top_apps, top_devices = await _run_all(
        db,
        (f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE is_archived = TRUE) AS archived,
                   COUNT(*) FILTER (
                       WHERE is_archived = FALSE
                         AND last_seen >= NOW() - (:cutoff * INTERVAL '1 second')
                   ) AS online,
                   COUNT(*) FILTER (
                       WHERE is_archived = FALSE
                         AND (last_seen IS NULL
                              OR last_seen < NOW() - (:cutoff * INTERVAL '1 second'))
                   ) AS offline
            FROM devices
            WHERE company_id = :cid AND is_deleted = FALSE{dev_scope}
        """, p_meta),
        (f"""
            SELECT platform, COUNT(*) AS cnt
            FROM   devices
            WHERE  company_id = :cid AND is_deleted = FALSE AND is_archived = FALSE{dev_scope}
            GROUP  BY platform ORDER BY cnt DESC
        """, p_plat),
        daily_q, apps_q, devices_q,
        timeout_ms=timeout_ms,
    )
    await _progress(on_progress, 90)

    if from_cache and not daily and _cache_answer_unsure(until):
        daily, top_apps, top_devices = await _run_all(db, *_time_queries(False), timeout_ms=timeout_ms)
    daily = sorted(daily, key=lambda r: r.day)
    counts = counts_rows[0]
    total_app_secs = sum(int(r.seconds or 0) for r in top_apps)
    return {
        "since": str(since),
        "until": str(until),
        "total_devices":    int(counts.total    or 0),
        "online_devices":   int(counts.online   or 0),
        "offline_devices":  int(counts.offline  or 0),
        "archived_devices": int(counts.archived or 0),
        "platform_breakdown": [
            {"platform": (r.platform or "").capitalize(), "count": int(r.cnt)}
            for r in platforms
        ],
        "daily_activity": [
            {
                "date":           str(r.day),
                "active_seconds": int(r.active       or 0),
                "idle_seconds":   int(r.idle         or 0),
                "device_count":   int(r.device_count or 0),
            }
            for r in daily
        ],
        "top_apps": [
            {
                "app_name": r.app_name,
                "seconds":  int(r.seconds or 0),
                "pct": round(int(r.seconds or 0) / total_app_secs * 100, 1)
                       if total_app_secs else 0,
            }
            for r in top_apps
        ],
        "most_active_devices": [
            {
                "device_id":      r.device_id,
                "hostname":       r.hostname,
                "employee_name":  r.employee_name,
                "active_seconds": int(r.active_seconds or 0),
            }
            for r in top_devices
        ],
    }
