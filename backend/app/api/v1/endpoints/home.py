"""Home-section dashboards: Organization Overview, Team Management, Activity
Dashboard. All endpoints are group-scoped (an RM sees only their groups) via the
shared scoping engine, and accept an optional ?group_id= filter.

Note: this product has no app/URL *productivity classification*, so these
dashboards report ACTIVITY (active time, activity status, app/site usage by time)
rather than ActivTrak-style productive/unproductive percentages.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require, get_current_user, get_current_device
from app.core.config import get_settings
from app.core.scoping import visible_device_ids, ALL
from app.core.group_tree import descendant_group_ids, devices_for_groups
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


@router.get("/overview")
async def overview(
    group_id:   str | None = Query(default=None),
    days:       int = Query(default=7, ge=1, le=90),
    since_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    until_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return {"total_devices": 0, "active_today": 0,
                "status": {"active": 0, "passive": 0, "inactive": 0},
                "avg_active_seconds_today": 0, "trend": []}

    rows = await _status_rows(ids, user.company_id, db)
    status_counts = {"active": 0, "passive": 0, "inactive": 0}
    for r in rows:
        status_counts[r["status"]] += 1

    frag, params = _clause(ids, "ae.device_id")
    params["company_id"] = str(user.company_id)

    # ── Date range for stat cards ─────────────────────────────────────────────
    # When since_date/until_date provided use that range; otherwise "today".
    if since_date and until_date:
        try:
            stat_since = datetime.strptime(since_date, "%Y-%m-%d").date()
            stat_until = datetime.strptime(until_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
        stat_filter = " AND ae.timestamp >= CAST(:stat_since AS date) AND ae.timestamp < (CAST(:stat_until AS date) + INTERVAL '1 day')"
        params["stat_since"] = stat_since
        params["stat_until"] = stat_until
    else:
        stat_filter = " AND ae.timestamp >= date_trunc('day', NOW())"

    # Active working time = window_change events for real user apps ONLY.
    # Excludes OS lock/login screen apps that appear as the foreground window
    # while the screen is locked, and any event whose app_name is NULL/blank.
    SYSTEM_IDLE_APPS = (
        "loginwindow",          # macOS lock screen
        "ScreenSaverEngine",    # macOS screen saver
        "System Idle Process",  # Windows idle placeholder
        "LockApp",              # Windows 10/11 lock screen
        "LogonUI",              # Windows login/credential UI
        "Idle", "screensaver",
    )
    placeholders = ", ".join(f":ex{i}" for i in range(len(SYSTEM_IDLE_APPS)))
    for i, name in enumerate(SYSTEM_IDLE_APPS):
        params[f"ex{i}"] = name

    stat_sql = f"""
        SELECT ae.device_id,
               SUM(CASE
                   WHEN ae.event_type = 'window_change'
                    AND ae.app_name IS NOT NULL
                    AND ae.app_name NOT IN ({placeholders})
                   THEN ae.duration ELSE 0
               END) AS active
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id{stat_filter}{frag}
        GROUP BY ae.device_id
    """
    stat_result = await db.execute(text(stat_sql), params)
    stat_rows = stat_result.fetchall()
    active_today = len([r for r in stat_rows if (r.active or 0) > 0])
    total_active_secs = sum(int(r.active or 0) for r in stat_rows)
    avg_active = int(total_active_secs / active_today) if active_today else 0

    # ── Trend — daily active seconds (always keyed by `days` param) ──────────
    params["days"] = days
    trend = await db.execute(
        text(f"""
            SELECT DATE(ae.timestamp) AS day,
                   SUM(CASE
                       WHEN ae.event_type = 'window_change'
                        AND ae.app_name IS NOT NULL
                        AND ae.app_name NOT IN ({placeholders})
                       THEN ae.duration ELSE 0
                   END) AS active
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}
            GROUP BY day ORDER BY day ASC
        """),
        params,
    )
    trend_out = [{"date": str(r.day), "active_seconds": int(r.active or 0)} for r in trend.fetchall()]

    return {
        "total_devices": len(rows),
        "active_today": active_today,
        "status": status_counts,
        "avg_active_seconds_today": avg_active,
        "trend": trend_out,
    }


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
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return []
    frag, params = _clause(ids, "ae.device_id")
    params.update({"company_id": str(user.company_id), "days": days, "limit": limit})
    res = await db.execute(
        text(f"""
            SELECT ae.app_name, SUM(ae.duration) AS seconds
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change' AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}
            GROUP BY ae.app_name ORDER BY seconds DESC LIMIT :limit
        """),
        params,
    )
    return [{"app_name": r.app_name, "seconds": int(r.seconds or 0)} for r in res.fetchall()]


@router.get("/web-usage")
async def web_usage(
    group_id: str | None = Query(default=None),
    days:     int = Query(default=1, ge=1, le=90),
    limit:    int = Query(default=12, ge=1, le=50),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return []
    frag, params = _clause(ids, "ae.device_id")
    params.update({"company_id": str(user.company_id), "days": days, "limit": limit})
    res = await db.execute(
        text(f"""
            SELECT substring(ae.url FROM '^https?://([^/]+)') AS host, COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit' AND ae.url IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}
            GROUP BY host HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
            ORDER BY visits DESC LIMIT :limit
        """),
        params,
    )
    return [{"host": r.host, "visits": int(r.visits or 0)} for r in res.fetchall()]


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
    MIN_PUSH_INTERVAL = 2.0

    async def _event_generator():
        q = broadcaster.subscribe()
        try:
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
