import asyncio
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require, get_current_user, get_current_device
from app.core.config import get_settings
from app.core.scoping import visible_device_ids, ALL
from app.core.group_tree import descendant_group_ids, devices_for_groups
from app.core.RedisCache import cache_get, cache_set
from app.db.models import Device, Group, Employee

router = APIRouter()
settings = get_settings()
log = logging.getLogger(__name__)

# Events that mean the user stepped away (vs. actively working).
_PASSIVE_EVENTS = ("lock", "idle_start")


async def _group_device_ids(group_id: str, company_id, db: AsyncSession) -> set:
    try:
        gid = uuid.UUID(group_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid group id")
    exists = await db.execute(
        select(Group.id).where(Group.id == gid, Group.company_id == company_id, Group.is_deleted == False)  # noqa: E712
    )
    if not exists.first():
        raise HTTPException(status_code=404, detail="Group not found")
    expanded = await descendant_group_ids([gid], company_id, db)
    device_ids = await devices_for_groups(expanded, db)
    return set(device_ids)


async def _resolve_ids(user: Employee, group_id: str | None, db: AsyncSession):
    """Return ALL or an explicit list of device ids = scope ∩ optional group."""
    vids = await visible_device_ids(user, db)
    if not group_id:
        return vids
    gset = await _group_device_ids(group_id, user.company_id, db)
    if vids is ALL:
        return list(gset)
    return list(set(vids) & gset)


def _clause(ids, col: str = "d.id"):
    """Build (sql_fragment, params) restricting col to ids; empty for ALL."""
    if ids is ALL:
        return "", {}
    return f" AND {col} = ANY(:hids)", {"hids": [str(i) for i in ids]}


async def _status_rows(ids, company_id, db: AsyncSession) -> list[dict]:
    """Live device status WITHOUT touching activity_events.

    Previously this ran two correlated LATERAL subqueries PER DEVICE into the
    (huge) activity_events table to find each device's latest event_type and
    current app. On the realtime/SSE path — polled every few seconds for the
    whole fleet — that was thousands of index probes into the biggest table on
    every call, the dominant load source behind requests failing under load.

    The ingest queue already keeps each device's latest_type + current_app in an
    in-memory cache (updated at enqueue time, BEFORE the DB write — so it is
    strictly fresher than any column we could read here). So we now select only
    the small devices table (one row per device, no JOIN into events) and derive
    status/current_app from that cache. A device that is online but missing from
    the cache (e.g. immediately after a backend restart, before its next ~5s
    post) shows as "active" and self-heals on the next heartbeat.
    """
    from app.core.ingest_queue import ingest_queue

    frag, params = _clause(ids, "d.id")
    params["company_id"] = str(company_id)
    rows = await db.execute(
        text(f"""
            SELECT d.id, d.hostname, d.platform, d.last_seen,
                   e.name AS employee_name,
                   EXTRACT(EPOCH FROM d.last_seen)::bigint AS last_seen_unix
            FROM devices d
            LEFT JOIN employees e ON e.id = d.employee_id
            WHERE d.company_id = :company_id AND d.is_active = TRUE{frag}
            ORDER BY d.last_seen DESC NULLS LAST
        """),
        params,
    )
    cache = ingest_queue.device_cache
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    out = []
    for r in rows.fetchall():
        did = str(r.id)
        cached = cache.get(did)
        cached_ts = cached.get("last_seen") if cached else None

        # Online if EITHER source is within the cutoff. The cache leads the DB
        # column (which lags by one async ingest-write cycle), so a device that
        # just posted is shown online immediately.
        db_online    = bool(r.last_seen and r.last_seen > cutoff)
        cache_online = bool(cached_ts and cached_ts > cutoff)
        last_seen_unix = int(r.last_seen_unix) if r.last_seen_unix else None
        if cache_online and (last_seen_unix is None or
                             (cached_ts and int(cached_ts.timestamp()) > last_seen_unix)):
            last_seen_unix = int(cached_ts.timestamp())

        if not (db_online or cache_online):
            st, current_app = "inactive", None
        elif cached and cached.get("latest_type") in _PASSIVE_EVENTS:
            st, current_app = "passive", None
        else:
            st = "active"
            current_app = cached.get("current_app") if cached else None

        out.append({
            "device_id": did,
            "hostname": r.hostname,
            "platform": r.platform,
            "employee_name": r.employee_name,
            "status": st,
            "current_app": current_app,
            "last_seen_unix": last_seen_unix,
        })
    return out


# ── SSE snapshot cache (fan-out overload protection) ──────────────────────────
# A device-update event fans out to EVERY open dashboard tab at once. Without
# coalescing, 1 500 tabs would each run the heavy double-LATERAL status query on
# every ingest flush and drain the 10-connection HTTP pool. But the overlaid
# payload is byte-identical for every connection sharing a (company, scope) —
# 1 500 admins on the company-wide view are ONE scope. So we compute the final
# SSE payload once per scope per _SNAPSHOT_TTL and let all same-scope tabs read it:
#   • single-flight lock → a simultaneous wake-up of N same-scope tabs = 1 query
#   • _snapshot_db_sem   → at most N concurrent SSE queries ever touch the pool,
#                          so SSE can never starve real user requests
#   • TTL pruning        → cache stays O(distinct live scopes); no unbounded growth
# The cache key includes company_id + the exact visible-id set, so a connection
# only ever reads a payload built for its OWN authorized scope (no leakage).
_SNAPSHOT_TTL = 1.0           # seconds — realtime freshness, far under the 60-90s cutoff
_SNAPSHOT_DB_CONCURRENCY = 4  # max concurrent SSE status queries (HTTP pool is 5+5)

_snapshot_cache: dict[tuple, tuple[float, str]] = {}
_snapshot_locks: dict[tuple, asyncio.Lock] = {}
_snapshot_db_sem = asyncio.Semaphore(_SNAPSHOT_DB_CONCURRENCY)


def _scope_key(ids, company_id) -> tuple:
    """Hashable cache key. Same authorized view ⇒ same key ⇒ shared payload."""
    return (str(company_id), "__ALL__" if ids is ALL else frozenset(str(i) for i in ids))


def _build_payload(rows: list[dict]) -> str:
    """Serialize the SSE frame. The cache overlay is already applied inside
    _status_rows, so this is a pure, fast serialization (orjson)."""
    return f"data: {orjson.dumps(rows).decode()}\n\n"


def _prune_snapshot_cache() -> None:
    """Drop expired entries and their idle locks so the cache can't grow without
    bound as scopes come and go. Cheap: only runs once the cache is non-trivial."""
    if len(_snapshot_cache) <= 64:
        return
    now = time.monotonic()
    for k in [k for k, (exp, _) in _snapshot_cache.items() if exp <= now]:
        _snapshot_cache.pop(k, None)
        lk = _snapshot_locks.get(k)
        if lk is not None and not lk.locked():
            _snapshot_locks.pop(k, None)


async def _scoped_status_payload(ids, company_id) -> str:
    """Return the SSE payload for a scope, cached for _SNAPSHOT_TTL and shared by
    every connection on the same scope (single-flight + bounded DB concurrency)."""
    from app.db.base import AsyncSessionLocal
    key = _scope_key(ids, company_id)

    hit = _snapshot_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    lock = _snapshot_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _snapshot_cache.get(key)          # re-check: another tab may have filled it
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with _snapshot_db_sem:            # cap concurrent SSE queries on the pool
            async with AsyncSessionLocal() as sess:
                rows = await _status_rows(ids, company_id, sess)
        payload = _build_payload(rows)
        _snapshot_cache[key] = (time.monotonic() + _SNAPSHOT_TTL, payload)

    _prune_snapshot_cache()
    return payload


# Excluded app names for active-time calculation — OS lock/login screens that
# appear as the foreground window while the device is locked. Defined once and
# passed as a Postgres array so the query is never rebuilt dynamically.
_SYSTEM_IDLE_APPS = [
    "loginwindow",
    "ScreenSaverEngine",
    "System Idle Process",
    "LockApp",
    "LogonUI",
    "Idle",
    "screensaver",
]

_resp_bg_keys: set[tuple] = set()                           # keys with an active bg refresh


async def _device_ids_for_scope(
    user: Employee, group_id: str | None, db: AsyncSession
) -> tuple[list[str], Any]:
    """Resolve device scope → (device_ids, scope_key).

    For ALL-scope admins we materialise the full company device list so every
    downstream query can use ``AND ae.device_id = ANY(:hids)`` and hit the
    partial index ``idx_events_window_device_ts (device_id, timestamp DESC)``
    instead of a broad JOIN+company filter that forces a full-table scan.
    The scope_key is a stable value safe to embed in a cache key.
    """
    ids = await _resolve_ids(user, group_id, db)
    if ids is ALL:
        rows = await db.execute(
            text("SELECT id::text FROM devices WHERE company_id = :cid AND is_active = TRUE"),
            {"cid": str(user.company_id)},
        )
        device_ids = [r[0] for r in rows.fetchall()]
        scope_key  = ("ALL", group_id or "")          # same company+group → same scope
    else:
        device_ids = [str(i) for i in ids]
        scope_key  = (frozenset(device_ids), group_id or "")  # exact device set
    return device_ids, scope_key


async def _compute_overview_inner(
    company_id_str: str,
    device_ids: list[str],
    days: int,
    stat_since,
    stat_until,
    today,
    db: AsyncSession,
    all_scope: bool = False,
) -> dict:
    """Core overview computation.

    Fast path (full-scope, standard windows): the daily trend comes from
    report_daily_summary and the per-device stat window from
    report_daily_device_cache — two tiny index lookups instead of aggregating
    days of activity_events per request (measured 40s live on a 42M-row table).
    Scoped/group views and cache misses fall back to the live aggregation,
    which uses ``device_ids = ANY(:hids)`` + the partial covering index.
    """
    rows = await _status_rows(device_ids, company_id_str, db)
    status_counts = {"active": 0, "passive": 0, "inactive": 0}
    for r in rows:
        status_counts[r["status"]] += 1

    if all_scope and days <= 31 and stat_since >= today - timedelta(days=31):
        from app.core import report_cache
        trend_rows = await report_cache.summary_from_cache(db, company_id_str, days)
        dev_active = await report_cache.device_active_from_cache(
            db, company_id_str, stat_since, stat_until,
        )
        if trend_rows is not None and dev_active is not None:
            active_today      = len([v for v in dev_active.values() if v > 0])
            total_active_secs = sum(dev_active.values())
            return {
                "total_devices":            len(rows),
                "active_today":             active_today,
                "status":                   status_counts,
                "avg_active_seconds_today": int(total_active_secs / active_today) if active_today else 0,
                "trend": [
                    {"date": str(r.day), "active_seconds": int(r.active_seconds or 0)}
                    for r in sorted(trend_rows, key=lambda r: r.day)
                ],
            }

    trend_start   = today - timedelta(days=days - 1)
    stat_in_trend = stat_since >= trend_start

    combined = await db.execute(text("""
        SELECT DATE(ae.timestamp) AS day, ae.device_id,
               SUM(CASE WHEN ae.app_name <> ALL(:excluded_apps) THEN ae.duration ELSE 0 END) AS active
        FROM activity_events ae
        WHERE ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          AND ae.device_id = ANY(:hids)
        GROUP BY day, ae.device_id
    """), {"hids": device_ids, "days": days, "excluded_apps": _SYSTEM_IDLE_APPS})

    daily_totals : dict = defaultdict(int)
    stat_by_device: dict = defaultdict(int)

    for r in combined.fetchall():
        active = int(r.active or 0)
        daily_totals[r.day] += active
        if stat_in_trend and stat_since <= r.day <= stat_until:
            stat_by_device[r.device_id] += active

    trend_out = [
        {"date": str(day), "active_seconds": total}
        for day, total in sorted(daily_totals.items())
    ]

    if not stat_in_trend:
        extra = {
            "hids": device_ids, "excluded_apps": _SYSTEM_IDLE_APPS,
            "stat_since": str(stat_since), "stat_until": str(stat_until),
        }
        stat_res = await db.execute(text("""
            SELECT ae.device_id,
                   SUM(CASE WHEN ae.app_name <> ALL(:excluded_apps) THEN ae.duration ELSE 0 END) AS active
            FROM activity_events ae
            WHERE ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:stat_since AS date)
              AND ae.timestamp < (CAST(:stat_until AS date) + INTERVAL '1 day')
              AND ae.device_id = ANY(:hids)
            GROUP BY ae.device_id
        """), extra)
        for r in stat_res.fetchall():
            stat_by_device[r.device_id] = int(r.active or 0)

    active_today      = len([v for v in stat_by_device.values() if v > 0])
    total_active_secs = sum(stat_by_device.values())
    avg_active        = int(total_active_secs / active_today) if active_today else 0

    return {
        "total_devices":            len(rows),
        "active_today":             active_today,
        "status":                   status_counts,
        "avg_active_seconds_today": avg_active,
        "trend":                    trend_out,
    }


async def _bg_refresh_overview(
    cache_key: tuple,
    company_id_str: str,
    device_ids: list[str],
    days: int,
    stat_since_str: str,
    stat_until_str: str,
    all_scope: bool = False,
) -> None:
    """Background task: recompute overview and silently update the cache."""
    from app.db.base import AsyncSessionLocal
    try:
        today      = datetime.now(timezone.utc).date()
        stat_since = datetime.strptime(stat_since_str, "%Y-%m-%d").date()
        stat_until = datetime.strptime(stat_until_str, "%Y-%m-%d").date()
        async with AsyncSessionLocal() as db:
            result = await _compute_overview_inner(
                company_id_str, device_ids, days, stat_since, stat_until, today, db,
                all_scope=all_scope,
            )
        await cache_set(cache_key, result)
    except Exception:
        log.exception("bg overview refresh failed for key %s", cache_key[:3])
    finally:
        _resp_bg_keys.discard(cache_key)


async def _compute_app_usage_inner(
    device_ids: list[str], days: int, limit: int, db: AsyncSession,
) -> list:
    """Core app-usage computation using device_id = ANY(:hids)."""
    res = await db.execute(text("""
        SELECT ae.app_name, SUM(ae.duration) AS seconds
        FROM activity_events ae
        WHERE ae.event_type = 'window_change' AND ae.app_name IS NOT NULL
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          AND ae.device_id = ANY(:hids)
        GROUP BY ae.app_name ORDER BY seconds DESC LIMIT :limit
    """), {"hids": device_ids, "days": days, "limit": limit})
    return [{"app_name": r.app_name, "seconds": int(r.seconds or 0)} for r in res.fetchall()]


async def _bg_refresh_app_usage(
    cache_key: tuple, device_ids: list[str], days: int, limit: int,
) -> None:
    """Background task: recompute app-usage and silently update the cache."""
    from app.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            result = await _compute_app_usage_inner(device_ids, days, limit, db)
        await cache_set(cache_key, result)
    except Exception:
        log.exception("bg app-usage refresh failed for key %s", cache_key[:2])
    finally:
        _resp_bg_keys.discard(cache_key)


async def _compute_widgets_inner(
    company_id_str: str,
    device_ids: list[str],
    app_days: int,
    app_limit: int,
    today,          # datetime.date
    db: AsyncSession,
    all_scope: bool = False,
) -> dict:
    """Compute all home-page widget data with a minimal number of DB round-trips.

    Fast path (full-scope): per-device active seconds come from
    report_daily_device_cache and top apps from tech_daily_app_cache — two tiny
    lookups (~ms) instead of aggregating today's ~2M events per request.

    Fallback (scoped views / cache stale): one MATERIALIZED CTE scan of
    activity_events from the start of the app_days window, exactly as before.
    """
    status_rows = await _status_rows(device_ids, company_id_str, db)
    status_counts = {"active": 0, "passive": 0, "inactive": 0}
    for r in status_rows:
        status_counts[r["status"]] += 1

    app_since = today - timedelta(days=app_days - 1)

    stat_by_device: dict = {}
    app_agg: dict = {}
    served_from_cache = False

    if all_scope:
        from app.core import report_cache, tech_cache
        dev_active = await report_cache.device_active_from_cache(
            db, company_id_str, today, today,
        )
        top_apps = await tech_cache.app_usage_from_cache(
            db, company_id_str, app_since, today, app_limit,
        )
        if dev_active is not None and top_apps is not None:
            stat_by_device = dev_active
            app_agg = {r.app_name: int(r.total_secs or 0) for r in top_apps}
            served_from_cache = True

    if not served_from_cache:
        combined = await db.execute(text("""
            WITH events AS MATERIALIZED (
                SELECT ae.device_id,
                       normalize_app_name(ae.app_name) AS app_name,
                       ae.duration,
                       DATE(ae.timestamp) AS event_date
                FROM   activity_events ae
                WHERE  ae.event_type = 'window_change'
                  AND  ae.app_name   IS NOT NULL
                  AND  ae.timestamp  >= :app_since
                  AND  ae.device_id  = ANY(:hids)
            ),
            device_active AS (
                SELECT device_id::text AS device_id_str,
                       SUM(CASE WHEN app_name <> ALL(:excluded_apps)
                                THEN duration ELSE 0 END) AS active_secs
                FROM   events
                WHERE  event_date = CURRENT_DATE
                GROUP  BY device_id
            ),
            top_apps AS (
                SELECT app_name,
                       SUM(duration) AS total_secs
                FROM   events
                GROUP  BY app_name
                ORDER  BY total_secs DESC
                LIMIT  :app_limit
            )
            SELECT 'd'           AS kind,
                   device_id_str AS device_id,
                   NULL::text    AS app_name,
                   active_secs   AS val
            FROM   device_active
            UNION ALL
            SELECT 'a',
                   NULL::text,
                   app_name,
                   total_secs
            FROM   top_apps
        """), {
            "hids":          device_ids,
            "app_since":     app_since,
            "excluded_apps": _SYSTEM_IDLE_APPS,
            "app_limit":     app_limit,
        })

        for r in combined.fetchall():
            val = int(r.val or 0)
            if r.kind == 'd':
                stat_by_device[r.device_id] = val
            else:
                app_agg[r.app_name] = val

    active_today      = len([v for v in stat_by_device.values() if v > 0])
    total_active_secs = sum(stat_by_device.values())
    avg_active        = int(total_active_secs / active_today) if active_today else 0

    return {
        "overview": {
            "total_devices":            len(status_rows),
            "active_today":             active_today,
            "status":                   status_counts,
            "avg_active_seconds_today": avg_active,
            "trend":                    [],
        },
        "app_usage": sorted(
            [{"app_name": k, "seconds": v} for k, v in app_agg.items()],
            key=lambda x: -x["seconds"],
        ),
    }


async def _bg_refresh_widgets(
    cache_key: tuple,
    company_id_str: str,
    device_ids: list[str],
    app_days: int,
    app_limit: int,
    all_scope: bool = False,
) -> None:
    """Background task: silently recompute widget data and update the cache."""
    from app.db.base import AsyncSessionLocal
    try:
        today = datetime.now(timezone.utc).date()
        async with AsyncSessionLocal() as db:
            result = await _compute_widgets_inner(
                company_id_str, device_ids, app_days, app_limit, today, db,
                all_scope=all_scope,
            )
        await cache_set(cache_key, result)
    except Exception:
        log.exception("bg widgets refresh failed for key %s", cache_key[:3])
    finally:
        _resp_bg_keys.discard(cache_key)


@router.get("/widgets")
async def widgets(
    group_id:  str | None = Query(default=None),
    app_days:  int = Query(default=1,  ge=1, le=90),
    app_limit: int = Query(default=12, ge=1, le=50),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Single endpoint: today's overview stats + top app usage in one round-trip.

    Scans activity_events ONCE via a MATERIALIZED CTE (device_active + top_apps)
    so both aggregations share a single sequential scan instead of two parallel
    scans on separate connections.  Wall-clock time ≈ 5 ms (status) + ~50 ms
    (today-only CTE) = ~55 ms vs the previous ~220 ms two-session approach.
    """
    device_ids, scope_key = await _device_ids_for_scope(user, group_id, db)
    if not device_ids:
        return {
            "overview":  {"total_devices": 0, "active_today": 0,
                          "status": {"active": 0, "passive": 0, "inactive": 0},
                          "avg_active_seconds_today": 0, "trend": []},
            "app_usage": [],
        }

    today     = datetime.now(timezone.utc).date()
    all_scope = scope_key[0] == "ALL"
    cache_key = ("widgets", str(user.company_id), scope_key, app_days, app_limit)

    cached, is_stale = await cache_get(cache_key)
    if cached is not None:
        if is_stale and cache_key not in _resp_bg_keys:
            _resp_bg_keys.add(cache_key)
            asyncio.create_task(_bg_refresh_widgets(
                cache_key, str(user.company_id), device_ids, app_days, app_limit,
                all_scope=all_scope,
            ))
        return cached

    result = await _compute_widgets_inner(
        str(user.company_id), device_ids, app_days, app_limit, today, db,
        all_scope=all_scope,
    )
    await cache_set(cache_key, result)
    return result


@router.get("/overview")
async def overview(
    group_id:   str | None = Query(default=None),
    days:       int = Query(default=7, ge=1, le=90),
    since_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    until_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    device_ids, scope_key = await _device_ids_for_scope(user, group_id, db)
    if not device_ids:
        return {"total_devices": 0, "active_today": 0,
                "status": {"active": 0, "passive": 0, "inactive": 0},
                "avg_active_seconds_today": 0, "trend": []}

    today = datetime.now(timezone.utc).date()
    if since_date and until_date:
        try:
            stat_since = datetime.strptime(since_date, "%Y-%m-%d").date()
            stat_until = datetime.strptime(until_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    else:
        stat_since = stat_until = today

    all_scope = scope_key[0] == "ALL"
    cache_key = ("overview", str(user.company_id), scope_key, days, str(stat_since), str(stat_until))
    cached, is_stale = await cache_get(cache_key)
    if cached is not None:
        if is_stale and cache_key not in _resp_bg_keys:
            _resp_bg_keys.add(cache_key)
            asyncio.create_task(_bg_refresh_overview(
                cache_key, str(user.company_id), device_ids,
                days, str(stat_since), str(stat_until),
                all_scope=all_scope,
            ))
        return cached

    result = await _compute_overview_inner(
        str(user.company_id), device_ids, days, stat_since, stat_until, today, db,
        all_scope=all_scope,
    )
    await cache_set(cache_key, result)
    return result


@router.get("/realtime")
async def realtime(
    group_id: str | None = Query(default=None),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Returns live device status.

    _status_rows already derives online/status/current_app from the in-memory
    ingest cache (fresher than the DB) over a single small devices-table scan —
    no per-device probes into activity_events — so this is just scope + serialize.
    """
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return []
    return await _status_rows(ids, user.company_id, db)


@router.get("/app-usage")
async def app_usage(
    group_id: str | None = Query(default=None),
    days:     int = Query(default=1, ge=1, le=90),
    limit:    int = Query(default=12, ge=1, le=50),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    device_ids, scope_key = await _device_ids_for_scope(user, group_id, db)
    if not device_ids:
        return []

    cache_key = ("app_usage", str(user.company_id), scope_key, days, limit)
    cached, is_stale = await cache_get(cache_key)
    if cached is not None:
        if is_stale and cache_key not in _resp_bg_keys:
            _resp_bg_keys.add(cache_key)
            asyncio.create_task(_bg_refresh_app_usage(cache_key, device_ids, days, limit))
        return cached

    result = await _compute_app_usage_inner(device_ids, days, limit, db)
    await cache_set(cache_key, result)
    return result


async def _compute_web_usage_inner(device_ids: list[str], days: int, limit: int, db: AsyncSession) -> list:
    res = await db.execute(text("""
        SELECT substring(ae.url FROM '^https?://([^/]+)') AS host, COUNT(*) AS visits
        FROM activity_events ae
        WHERE ae.event_type = 'url_visit' AND ae.url IS NOT NULL
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          AND ae.device_id = ANY(:hids)
        GROUP BY host HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
        ORDER BY visits DESC LIMIT :limit
    """), {"hids": device_ids, "days": days, "limit": limit})
    return [{"host": r.host, "visits": int(r.visits or 0)} for r in res.fetchall()]


async def _bg_refresh_web_usage(cache_key: tuple, company_id_str: str, device_ids: list[str], days: int, limit: int) -> None:
    from app.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            result = await _compute_web_usage_inner(device_ids, days, limit, db)
        await cache_set(cache_key, result)
    except Exception:
        log.exception("bg web-usage refresh failed for key %s", cache_key[:3])
    finally:
        _resp_bg_keys.discard(cache_key)


@router.get("/web-usage")
async def web_usage(
    group_id: str | None = Query(default=None),
    days:     int = Query(default=1, ge=1, le=90),
    limit:    int = Query(default=12, ge=1, le=50),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    device_ids, scope_key = await _device_ids_for_scope(user, group_id, db)
    if not device_ids:
        return []

    cache_key = ("web_usage", str(user.company_id), scope_key, days, limit)
    cached, is_stale = await cache_get(cache_key)
    if cached is not None:
        if is_stale and cache_key not in _resp_bg_keys:
            _resp_bg_keys.add(cache_key)
            asyncio.create_task(_bg_refresh_web_usage(cache_key, str(user.company_id), device_ids, days, limit))
        return cached

    result = await _compute_web_usage_inner(device_ids, days, limit, db)
    await cache_set(cache_key, result)
    return result


# ── Meeting logs ──────────────────────────────────────────────────────────────
# Apps that represent the lock/login screen — never count toward meeting active
# time (they emit window_change while the screen is locked).
_MEETING_EXCLUDED_APPS = frozenset({
    "loginwindow", "ScreenSaverEngine", "System Idle Process",
    "LockApp", "LogonUI", "Idle", "screensaver",
})


def _pair_meeting_sessions(events, now_ts):
    """Pair an ordered stream of meeting_start/meeting_end events into sessions.

    events: iterable of (device_id, event_type, platform, title, ts) ordered by
            (device_id, ts). Pure — robust to a missing start or end:
      • start while one is already open → the open one closes at the new start,
      • end with nothing open → ignored,
      • still-open at the end → marked ongoing and closed at now_ts.
    """
    sessions = []
    open_by_dev = {}
    for dev, etype, platform, title, ts in events:
        if etype == "meeting_start":
            prev = open_by_dev.get(dev)
            if prev is not None:
                prev["end_ts"] = ts
                sessions.append(prev)
            open_by_dev[dev] = {
                "device_id": dev, "platform": platform or "Meeting",
                "title": title or "", "start_ts": ts, "end_ts": None, "ongoing": False,
            }
        else:  # meeting_end
            prev = open_by_dev.pop(dev, None)
            if prev is not None:
                prev["end_ts"] = ts
                sessions.append(prev)
    for prev in open_by_dev.values():
        prev["end_ts"] = now_ts
        prev["ongoing"] = True
        sessions.append(prev)
    return sessions


def _enrich_sessions(sessions, window_changes):
    """Compute each session's active seconds + apps-used from window_change slices
    overlapping its [start_ts, end_ts] (same device). Pure. Idle/lock contribute
    nothing because they emit no window_change; locked-app slices are excluded."""
    for s in sessions:
        secs, apps = 0, {}
        for dev, app, dur, ts in window_changes:
            if dev != s["device_id"]:
                continue
            if not (s["start_ts"] <= ts <= s["end_ts"]):
                continue
            if not app or app in _MEETING_EXCLUDED_APPS:
                continue
            secs += dur
            apps[app] = apps.get(app, 0) + dur
        s["active_secs"] = int(secs)
        s["duration_secs"] = int(s["end_ts"] - s["start_ts"])
        s["apps"] = sorted(
            ({"app_name": a, "seconds": int(d)} for a, d in apps.items()),
            key=lambda x: -x["seconds"],
        )[:5]
    return sessions


@router.get("/meetings")
async def meetings(
    device_id:   str | None = Query(default=None, description="Restrict to one device"),
    employee_id: str | None = Query(default=None, description="Restrict to one user's devices"),
    group_id:    str | None = Query(default=None),
    days:        int = Query(default=1, ge=1, le=31),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Meeting sessions for the visible devices over the last `days`.

    A session is bounded by a meeting_start/meeting_end pair; its active time is
    the window_change activity within the session (so other apps used during the
    meeting count, while idle/locked time does not). Trivial sessions (<30s active)
    are dropped as noise. Scoping mirrors the other home endpoints — company +
    the caller's visible devices, optionally narrowed to one device or employee.
    """
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return {"total_active_secs": 0, "count": 0, "sessions": []}

    frag, params = _clause(ids, "ae.device_id")
    params.update({"company_id": str(user.company_id), "days": days})
    extra = ""
    if device_id:
        extra += " AND ae.device_id = :only_device"
        params["only_device"] = device_id
    if employee_id:
        extra += " AND d.employee_id = :only_emp"
        params["only_emp"] = employee_id

    mres = await db.execute(
        text(f"""
            SELECT ae.device_id::text AS device_id, ae.event_type,
                   ae.app_name, ae.window_title,
                   EXTRACT(EPOCH FROM ae.timestamp)::bigint AS ts
            FROM activity_events ae JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type IN ('meeting_start', 'meeting_end')
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}{extra}
            ORDER BY ae.device_id, ae.timestamp ASC
        """),
        params,
    )
    mevents = [(r.device_id, r.event_type, r.app_name, r.window_title, int(r.ts))
               for r in mres.fetchall()]
    if not mevents:
        return {"total_active_secs": 0, "count": 0, "sessions": []}

    wres = await db.execute(
        text(f"""
            SELECT ae.device_id::text AS device_id, ae.app_name, ae.duration,
                   EXTRACT(EPOCH FROM ae.timestamp)::bigint AS ts
            FROM activity_events ae JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change' AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}{extra}
        """),
        params,
    )
    wchanges = [(r.device_id, r.app_name, int(r.duration or 0), int(r.ts))
                for r in wres.fetchall()]

    now_ts = int(datetime.now(timezone.utc).timestamp())
    sessions = _enrich_sessions(_pair_meeting_sessions(mevents, now_ts), wchanges)
    sessions = [s for s in sessions if s["active_secs"] >= 30 or s["ongoing"]]
    sessions.sort(key=lambda s: s["start_ts"], reverse=True)

    return {
        "total_active_secs": int(sum(s["active_secs"] for s in sessions)),
        "count": len(sessions),
        "sessions": sessions,
    }


@router.get("/my-stats")
async def my_stats(
    user: Employee = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Personal stats for the logged-in user — scoped to their own devices."""
    device_rows = await db.execute(
        select(Device.id).where(Device.employee_id == user.id, Device.is_active == True)  # noqa: E712
    )
    device_ids = [str(r[0]) for r in device_rows.all()]

    if not device_ids:
        return {"active_seconds_today": 0, "top_apps": [], "recent_events": [], "device_count": 0}

    active_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(
                CASE WHEN event_type = 'window_change' THEN duration ELSE 0 END
            ), 0) AS active
            FROM activity_events
            WHERE device_id = ANY(:ids) AND timestamp >= date_trunc('day', NOW())
        """),
        {"ids": device_ids},
    )
    active_seconds = int(active_result.scalar() or 0)

    apps_result = await db.execute(
        text("""
            SELECT app_name, SUM(duration) AS seconds
            FROM activity_events
            WHERE device_id = ANY(:ids)
              AND event_type = 'window_change' AND app_name IS NOT NULL
              AND timestamp >= date_trunc('day', NOW())
            GROUP BY app_name ORDER BY seconds DESC LIMIT 5
        """),
        {"ids": device_ids},
    )
    top_apps = [{"app_name": r.app_name, "seconds": int(r.seconds or 0)} for r in apps_result.fetchall()]

    events_result = await db.execute(
        text("""
            SELECT event_type, app_name, window_title, url, duration,
                   EXTRACT(EPOCH FROM timestamp)::bigint AS ts_unix
            FROM activity_events WHERE device_id = ANY(:ids)
            ORDER BY timestamp DESC LIMIT 10
        """),
        {"ids": device_ids},
    )
    recent_events = [
        {
            "event_type": r.event_type, "app_name": r.app_name,
            "window_title": r.window_title, "url": r.url,
            "duration": r.duration,
            "timestamp_unix": int(r.ts_unix) if r.ts_unix else None,
        }
        for r in events_result.fetchall()
    ]

    return {
        "active_seconds_today": active_seconds,
        "top_apps": top_apps,
        "recent_events": recent_events,
        "device_count": len(device_ids),
    }


# ── Device active-time sync — called by the agent on startup ──────────────────

@router.get("/stream")
async def stream(
    group_id: str | None = Query(default=None),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    """
    SSE endpoint — pushes the realtime device list whenever ingest lands new data.

    Industrial-standard pattern:
    - Auth and scope resolved before streaming; DB connection released immediately.
    - No subscriber cap: events fan out via Redis pub/sub (app/core/broadcaster.py),
      so this scales across workers/replicas. Connection limits belong at the
      proxy/uvicorn/OS layer, not here.
    - Initial snapshot sent on connect so the client never shows a blank state.
    - Fan-out safe: the status query is computed once per (company, scope) and
      shared by all same-scope tabs (single-flight + bounded DB concurrency), so
      N open dashboards do NOT become N queries per event.
    - 25 s heartbeat keeps proxies and load-balancers from killing idle connections.
    - CancelledError caught explicitly so client disconnect is always clean.
    """
    from app.core import broadcaster

    ids        = await _resolve_ids(user, group_id, db)
    company_id = user.company_id

    # CRITICAL: release the request's pooled connection NOW. This response
    # streams for the life of the dashboard tab — holding the dependency
    # session would pin one connection per viewer and drain the pool (the
    # "QueuePool limit reached" production jam). The shared payload cache uses
    # its own short-lived sessions.
    await db.close()

    async def _payload() -> str:
        # Scope-keyed, single-flight cache: every same-scope tab that wakes on
        # this event reuses ONE query+build instead of each hammering the pool.
        return await _scoped_status_payload(ids, company_id)

    # Per-subscriber push throttle. The heavy DB work is already coalesced by the
    # shared scope cache; this just bounds how often each tab re-renders.
    MIN_PUSH_INTERVAL = 0.5

    async def _event_generator():
        q = broadcaster.subscribe()
        try:
            # Flush the stream open IMMEDIATELY (before the first status query) so
            # the browser's EventSource leaves "pending" and fires onopen at once.
            # On a cold-cache scope the snapshot query below can take a few ms; we
            # don't want the connection itself to appear slow while it runs. The
            # client ignores comment frames (it only acts on "data: " lines), so
            # this changes nothing visible — it just unblocks the connection and
            # forces any proxy buffer to flush right away.
            yield ": connected\n\n"

            yield await _payload()
            last_push = time.monotonic()

            while True:
                try:
                    await asyncio.wait_for(q.get(), timeout=25.0)
                    # Coalesce the burst and enforce the minimum push interval
                    delay = MIN_PUSH_INTERVAL - (time.monotonic() - last_push)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    while not q.empty():
                        q.get_nowait()
                    yield await _payload()
                    last_push = time.monotonic()
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Transient payload/query error: keep the connection alive with
                    # a heartbeat rather than letting the stream die (and 500 mid-
                    # response). The next event retries the query.
                    log.exception("home stream loop error — continuing")
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass  # client disconnected — finally block handles cleanup
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/device-today-active")
async def device_today_active(
    since: int | None = Query(
        default=None,
        description="Unix epoch (UTC seconds) of the DEVICE's local midnight. The "
                    "agent sends this so 'today' matches the user's local day exactly, "
                    "independent of the server/DB timezone.",
    ),
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns today's authoritative active-work seconds for this device,
    calculated from stored activity_events (same formula the portal uses).

    The agent calls this on startup so the widget baseline always matches
    the portal — no drift from local daily_totals accumulation errors.

    Day boundary: when the agent supplies `since` (its local midnight as an
    absolute unix timestamp) we filter `timestamp >= to_timestamp(since)`, which
    is unambiguous and matches the device's local day. The previous boundary
    (`date_trunc('day', NOW() AT TIME ZONE 'UTC')`) both (a) used the UTC day
    rather than the device's local day, and (b) re-applied the DB session
    timezone on comparison — on a non-UTC production DB that shifted the boundary
    by the session offset (e.g. +1h on a UTC+1 server), inflating "today" and
    making the agent jump forward on restart. `to_timestamp` avoids both.
    The no-`since` fallback computes a correct UTC-midnight timestamptz.
    """
    if since is not None:
        boundary_sql = "ae.timestamp >= to_timestamp(:since)"
        params = {"did": str(device.id), "since": int(since)}
    else:
        # Backward-compatible fallback (older agents): correct UTC-midnight,
        # session-timezone-independent.
        boundary_sql = "ae.timestamp >= (date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
        params = {"did": str(device.id)}

    result = await db.execute(
        text(f"""
            SELECT COALESCE(SUM(
                CASE
                    WHEN ae.event_type = 'window_change'
                     AND ae.app_name IS NOT NULL
                     AND ae.app_name NOT IN (
                         'loginwindow','ScreenSaverEngine',
                         'System Idle Process','LockApp','LogonUI','Idle','screensaver'
                     )
                    THEN ae.duration ELSE 0
                END
            ), 0) AS active_secs
            FROM activity_events ae
            WHERE ae.device_id = :did
              AND {boundary_sql}
        """),
        params,
    )
    row = result.fetchone()
    return {"active_seconds_today": int(row.active_secs or 0)}
