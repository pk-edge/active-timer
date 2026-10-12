"""Single home for the device_full and fleet report SQL.

The same queries used to be copy-pasted into the background workers, the
Redis-down synchronous fallback in POST /jobs, and the on-screen GET endpoints.
They now all call build_device_full_report / build_fleet_report so the numbers
an export shows are, by construction, the numbers the page shows.

Session handling: pass `db` to run the queries sequentially on that one session
(request-scoped callers; an AsyncSession cannot run concurrent statements), or
leave it None to run them concurrently, each on its own AsyncSessionLocal
session (background workers, which have no request session).

Scoping is the caller's job: resolve visible_device_ids() while the request
session and `admin` are alive and hand the result in as `device_ids`
(None = every device in the company, list = only those; [] = nothing).
"""
import asyncio
import hashlib
from datetime import date
from typing import Awaitable, Callable, Optional

from sqlalchemy import text

from app.core.report_cache import _IDLE_APPS_SQL
from app.db.base import AsyncSessionLocal

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


# The engine's session-level statement_timeout (DB_STATEMENT_TIMEOUT_MS, 120 s)
# is sized for request traffic; a multi-year activity_events scan can legitimately
# run longer. Same value report_cache.py uses for its long scans.
_REPORT_STATEMENT_TIMEOUT = "SET LOCAL statement_timeout = '300000'"

# Caps how many report queries hold a pooled connection at once, across every
# concurrent job in this process. Jobs run in the API process on the shared
# engine (pool 5 + overflow 5); without a cap a few overlapping exports would
# take every connection and make ordinary requests time out waiting for one.
_REPORT_DB_SLOTS = asyncio.Semaphore(2)

Query = tuple[str, dict]  # (sql, params)


async def _fetch(db, sql: str, params: dict) -> list:
    """Run one statement and return all rows — on `db` if given, else on a
    private short-lived session so several _fetch calls can run in parallel."""
    if db is not None:
        return (await db.execute(text(sql), params)).fetchall()
    async with _REPORT_DB_SLOTS:
        async with AsyncSessionLocal() as own:
            await own.execute(text(_REPORT_STATEMENT_TIMEOUT))
            return (await own.execute(text(sql), params)).fetchall()


async def _run_all(db, *queries: Query) -> list:
    """Run (sql, params) queries. Concurrent when each owns its session
    (db is None), otherwise sequential on the shared session. Coroutines are
    created only when they are about to run, so a failure leaves none unawaited;
    on the concurrent path the first failure cancels the sibling queries."""
    if db is not None:
        await db.execute(text(_REPORT_STATEMENT_TIMEOUT))  # lasts for this transaction
        return [await _fetch(db, sql, params) for sql, params in queries]

    tasks = [asyncio.ensure_future(_fetch(None, sql, params)) for sql, params in queries]
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
) -> Optional[dict]:
    """DeviceFullReportOut-shaped dict for one device, or None if the device
    doesn't exist in the company. The caller must have already checked the
    device is visible to the requester."""
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

    daily, apps_raw, top_urls = await _run_all(
        db,
        (f"""
            SELECT DATE(ae.timestamp) AS day,
                   SUM({_ACTIVE_CASE}) AS active,
                   SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle
            FROM   activity_events ae
            WHERE  ae.device_id = :did
              AND  {_RANGE}
            GROUP  BY day ORDER BY day
        """, params),
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
    )
    await _progress(on_progress, 90)

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
) -> dict:
    """FleetReportOut-shaped dict restricted to `device_ids` (None = whole
    company). Every query — device counts, platform split, daily activity, top
    apps, top devices — carries the same scope, so a scoped caller can neither
    see nor infer devices outside it."""
    await _progress(on_progress, 5)

    # Same binding as the roster / _scope_clause: plain list[str] against a uuid column.
    dev_scope = "" if device_ids is None else " AND id = ANY(:vids)"
    ae_scope = "" if device_ids is None else " AND ae.device_id = ANY(:vids)"
    vids = {} if device_ids is None else {"vids": list(device_ids)}

    p_meta = {"cid": company_id, "cutoff": ONLINE_CUTOFF_SECS, **vids}
    p_plat = {"cid": company_id, **vids}
    p_time = {"cid": company_id, "since": since, "until": until, **vids}

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
        (f"""
            SELECT DATE(ae.timestamp) AS day,
                   SUM({_ACTIVE_CASE}) AS active,
                   SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle,
                   COUNT(DISTINCT ae.device_id) AS device_count
            FROM   activity_events ae
            JOIN   devices d ON d.id = ae.device_id
            WHERE  d.company_id = :cid
              AND  {_RANGE}{ae_scope}
            GROUP  BY day ORDER BY day
        """, p_time),
        (f"""
            SELECT normalize_app_name(ae.app_name) AS app_name,
                   SUM(ae.duration) AS seconds
            FROM   activity_events ae
            JOIN   devices d ON d.id = ae.device_id
            WHERE  d.company_id = :cid
              AND  ae.event_type = 'window_change'
              AND  ae.app_name IS NOT NULL
              AND  {_RANGE}{ae_scope}
              {_EXCL_IDLE}
            GROUP  BY normalize_app_name(ae.app_name)
            ORDER  BY seconds DESC LIMIT 20
        """, p_time),
        (f"""
            SELECT d.id::text AS device_id,
                   d.hostname,
                   e.name     AS employee_name,
                   SUM({_ACTIVE_CASE}) AS active_seconds
            FROM   activity_events ae
            JOIN   devices   d ON d.id = ae.device_id
            LEFT JOIN employees e ON e.id = d.employee_id
            WHERE  d.company_id = :cid
              AND  {_RANGE}{ae_scope}
            GROUP  BY d.id, d.hostname, e.name
            ORDER  BY active_seconds DESC LIMIT 10
        """, p_time),
    )
    await _progress(on_progress, 90)

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
