import asyncio
import uuid
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.api.deps import get_db, get_current_admin, require
from app.core.scoping import visible_device_ids, ALL
from app.core import report_cache
from app.core.RedisCache import (
    cache_get, cache_set, FRESH_ANALYTICS, STALE_ANALYTICS, FRESH_DETAIL, STALE_DETAIL,
    FAILURE_TTL, cache_clear_failure, cache_get_failure, cache_set_failure,
    release, try_acquire, wait_or_take,
)
from app.core.RedisClient import get_client
from app.db.models import Device, Employee
from app.schemas.report import CompanyDayOut, AppUsageOut, UrlVisitOut
from app.services.ReportQueries import (
    _EXCL_IDLE, LIVE_STATEMENT_TIMEOUT_MS, ONLINE_CUTOFF_SECS,
    build_device_full_report, build_fleet_report, fleet_cache_key,
    live_daily_summary, live_top_apps,
)

router = APIRouter()
log = logging.getLogger(__name__)

# _EXCL_IDLE (idle/lock-screen exclusion), ONLINE_CUTOFF_SECS and the live
# summary / top-apps aggregations live in app/services/ReportQueries.py so the
# on-screen endpoints and the exported reports share one definition.

# Longest span a device_full/fleet report may cover, inclusive. 732 = two years
# plus one leap day (a 2-year window holds at most one), so the dashboard's
# "Last 2 Years" preset always fits. Mirrored in dashboard/src/lib/reportRange.ts.
MAX_REPORT_RANGE_DAYS = 732


async def _scope_clause(admin: Employee, db: AsyncSession) -> tuple[str, dict]:
    """Return an SQL fragment + params restricting ae.device_id to the user's scope.

    Empty for scope='all'. For scoped users, an empty id list yields no rows
    (ANY('{}') matches nothing), which correctly shows an RM-with-no-groups nothing.
    """
    ids = await visible_device_ids(admin, db)
    if ids is ALL:
        return "", {}
    return " AND ae.device_id = ANY(:vids)", {"vids": [str(i) for i in ids]}


def _validate_range(days: int, since: Optional[date], until: Optional[date]) -> None:
    """Validation for the days-vs-since/until precedence on the four report
    endpoints below.

    Rule (deliberately simple and unambiguous, not a silent-precedence guess):
    - since/until must be supplied TOGETHER — one without the other is 400.
    - since must not be after until — 400.
    - the inclusive since..until span is capped at MAX_REPORT_RANGE_DAYS (the
      same cap the exports use) — 400, so an unbounded range can never reach
      the live fallback.
    - since/until are mutually exclusive with `days` being anything other than
      its own default (7): if the caller sends since/until AND an explicit
      non-default `days`, that's two conflicting ways to select the window in
      the same request — 400 rather than silently picking one. (A `days=7`
      sent alongside since/until is indistinguishable from `days` simply being
      left at its default, since FastAPI gives us no way to tell "omitted" from
      "explicitly 7" for a query param with a default — that ambiguity is
      accepted and since/until wins in that one case.)
    """
    if (since is None) != (until is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="since and until must be provided together",
        )
    if since is not None and until is not None:
        if since > until:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="since must not be after until",
            )
        if (until - since).days + 1 > MAX_REPORT_RANGE_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Date range must not exceed {MAX_REPORT_RANGE_DAYS} days",
            )
        if days != 7:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="days must be left at its default when since/until are provided",
            )


def _resolve_window(days: int, since: Optional[date], until: Optional[date]) -> tuple[date, date]:
    """The inclusive calendar range [since, until] a request selects.

    since/until win when given. Otherwise `days` means "the last N calendar days
    ending today (UTC)" — exactly what the DateRangePicker's "Last N Days"
    presets show, and the same day buckets report_daily_summary stores, so the
    on-screen numbers, the cached fast path and an export of the same range all
    cover identical rows. (This used to be a NOW()-based N*24h window whose
    oldest day was partial, which no export could reproduce.)
    """
    if since is not None and until is not None:
        return since, until
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=days - 1), today


def _window_clause(days: int, since: Optional[date], until: Optional[date]) -> tuple[str, dict]:
    """SQL fragment + bound params selecting _resolve_window()'s range over
    ae.timestamp — half-open [since, until + 1 day), the same sargable idiom the
    report jobs use (app/services/ReportQueries.py)."""
    since, until = _resolve_window(days, since, until)
    return (
        "ae.timestamp >= CAST(:since AS date) AND ae.timestamp < (CAST(:until AS date) + INTERVAL '1 day')",
        {"since": since, "until": until},
    )


def _range_cache_fp(days: int, since: Optional[date], until: Optional[date]) -> tuple:
    """Cache-key fragment: the resolved calendar range, so a `days` request and
    the equivalent since/until request share an entry, and a `days` entry
    written before midnight is never served for the next day's window."""
    since, until = _resolve_window(days, since, until)
    return (str(since), str(until))


def _parse_date(s: str, param: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date for {param}; expected YYYY-MM-DD")


def _validate_job_range(since_str: Optional[str], until_str: Optional[str]) -> tuple[date, date]:
    """Validate the since/until of a device_full or fleet report (job or GET).

    Both required, ISO dates, since <= until, until not after today (UTC), and
    the inclusive span at most MAX_REPORT_RANGE_DAYS. Every failure is a 400
    with a human-readable detail the dashboard can show as-is.
    """
    if not since_str or not until_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="since and until are both required",
        )
    since = _parse_date(since_str, "since")
    until = _parse_date(until_str, "until")
    if since > until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="since must not be after until",
        )
    if until > datetime.now(timezone.utc).date():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="until must not be in the future",
        )
    if (until - since).days + 1 > MAX_REPORT_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Date range must not exceed {MAX_REPORT_RANGE_DAYS} days",
        )
    return since, until


async def _get_visible_device(device_id: str, admin: Employee, db: AsyncSession) -> Device:
    """Fetch a device by id, enforcing company + visibility scope in one pass."""
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    ids = await visible_device_ids(admin, db)
    q = select(Device).where(
        Device.id == device_uuid,
        Device.company_id == admin.company_id,
        Device.is_deleted == False,  # noqa: E712
    )
    if ids is not ALL:
        q = q.where(Device.id.in_(ids))
    device = (await db.execute(q)).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


async def _scope_ids(admin: Employee, db: AsyncSession) -> Optional[list[str]]:
    """The caller's visible device ids as a list[str], or None for scope='all'."""
    ids = await visible_device_ids(admin, db)
    return None if ids is ALL else [str(i) for i in ids]


# ── Live fallbacks: statement budget, single-flight, negative caching ────────
# The per-day cache (app/core/report_cache.py) answers every picker range for a
# healthy worker. These guard the fallback path, which can be a multi-second
# company-wide aggregation:
#   * a tighter statement_timeout than the engine's 120 s, so a doomed query
#     frees its connection sooner;
#   * single-flight (RedisCache lock) so N concurrent identical requests run ONE
#     query — losers wait for the winner's cached value;
#   * a short-lived failure marker per exact cache key, so after a timeout the
#     identical request gets 503 + Retry-After immediately instead of burning
#     another full timeout. Validation errors (4xx) never reach this code.

# LIVE_STATEMENT_TIMEOUT_MS (60 s, ReportQueries) is the per-statement budget of
# every request-path compute; a statement that hits it is "the query is too
# heavy" and is what the failure marker below is for. A request-path compute may
# run several statements back to back, so it is also bounded in total.
_LIVE_COMPUTE_BUDGET_SECS = 80.0
_LIVE_LOCK_TTL  = 90      # holder-crash ceiling; MUST exceed _LIVE_COMPUTE_BUDGET_SECS
_LIVE_WAIT_SECS = 45.0    # how long a loser waits for the winner's value
_POOL_RETRY_AFTER = 5     # pool checkout timeout: retry soon, nothing is "heavy"

assert _LIVE_LOCK_TTL > _LIVE_COMPUTE_BUDGET_SECS > LIVE_STATEMENT_TIMEOUT_MS / 1000


class _BudgetExceeded(Exception):
    """The request-path compute ran past _LIVE_COMPUTE_BUDGET_SECS in total."""


def _unavailable(retry_after: int) -> HTTPException:
    n = max(int(retry_after), 1)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"This report is taking too long \u2014 try a shorter range or retry in {n} s",
        headers={"Retry-After": str(n)},
    )


def _busy() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"The server is busy \u2014 retry in {_POOL_RETRY_AFTER} s",
        headers={"Retry-After": str(_POOL_RETRY_AFTER)},
    )


def _exc_chain(exc: BaseException):
    """exc plus its wrapped causes (SQLAlchemy .orig, __cause__, __context__)."""
    seen, stack = set(), [exc]
    while stack and len(seen) < 8:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        stack += [getattr(e, "orig", None), e.__cause__, e.__context__]


def _is_query_canceled(exc: BaseException) -> bool:
    """True for a statement cancelled by statement_timeout (or an explicit
    cancel): asyncpg QueryCanceledError, directly or wrapped in a SQLAlchemy
    DBAPIError/OperationalError, or any error carrying sqlstate 57014."""
    try:
        from asyncpg.exceptions import QueryCanceledError
    except Exception:  # noqa: BLE001
        QueryCanceledError = ()  # type: ignore[assignment]
    for e in _exc_chain(exc):
        if QueryCanceledError and isinstance(e, QueryCanceledError):
            return True
        if getattr(e, "sqlstate", None) == "57014" or getattr(e, "pgcode", None) == "57014":
            return True
    return False


def _is_pool_timeout(exc: BaseException) -> bool:
    """SQLAlchemy QueuePool checkout timeout (no free connection within
    DB_POOL_TIMEOUT): the pool is busy, the query was never the problem."""
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    return any(isinstance(e, PoolTimeout) for e in _exc_chain(exc))


async def _bounded(coro, secs: float):
    """Await `coro`, raising _BudgetExceeded (and cancelling it) past `secs`.
    Done with an explicit task so an unrelated TimeoutError raised INSIDE the
    compute is never mistaken for the budget."""
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=secs)
    except BaseException:           # the request itself was cancelled
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise _BudgetExceeded()
    return task.result()


async def _refuse_if_failed(ckey: tuple) -> None:
    """503 straight away while a failure marker exists for this exact request."""
    remaining = await cache_get_failure(ckey)
    if remaining is not None:
        raise _unavailable(remaining)


async def _single_flight(ckey: tuple, compute, *, db=None, honor_failure: bool = True):
    """Run `compute()` once per `ckey` across concurrent callers.

    Returns (payload, shared): shared=True when the value came from the winner's
    cache entry instead of this caller's own run. `compute` must CACHE its own
    result before returning: the lock is released only after it returns, so a
    loser that sees the lock vanish can rely on the value already being there.

    Losers wait for the winner and — if the winner disappears without a value
    (cancelled client, crashed worker, error) — re-acquire the lock and compute
    themselves instead of sitting out the whole wait. The lock is an
    optimisation, never a gate (see RedisCache): after the wait times out a loser
    computes unlocked. Pass `db` so a waiting loser hands its pooled connection
    back first: ten waiters must not drain the HTTP pool. The lock is released
    with its token, so an expired holder can never delete a successor's lock.
    """
    token = await try_acquire(ckey, ttl=_LIVE_LOCK_TTL)
    if not token:
        if db is not None:
            await db.rollback()
        shared, token = await wait_or_take(
            ckey, _LIVE_WAIT_SECS, check_failure=honor_failure, ttl=_LIVE_LOCK_TTL)
        if shared is not None:
            return shared, True
        if token is None and honor_failure:
            await _refuse_if_failed(ckey)
    try:
        return await compute(), False
    finally:
        if token:
            await release(ckey, token)


async def _run_live(db, ckey: tuple, compute, *, fresh: int, stale: int,
                    single_flight: bool = False):
    """Compute an endpoint's payload live, cache it, and map failures:

      * statement timeout / query cancelled / the total budget: write the 30 s
        failure marker (an identical request would just time out again) and 503
        + Retry-After;
      * pool checkout timeout: 503 + Retry-After 5, NO marker (the DB is fine,
        the pool is momentarily busy — marking would lock every admin of the
        company out for 30 s);
      * anything else (transient DB error, code bug): roll back and propagate as
        a normal 500, no marker.

    `compute` is an async callable returning the JSON payload. Callers must have
    hoisted every ORM scalar they still need: a failure (or a waiting loser)
    rolls the session back, which expires loaded instances."""
    await _refuse_if_failed(ckey)

    async def _compute_and_store():
        # Marker, cache write and (via _single_flight) lock release all happen in
        # this order: a loser woken by the lock vanishing finds the value / marker.
        try:
            payload = await _bounded(compute(), _LIVE_COMPUTE_BUDGET_SECS)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            if _is_pool_timeout(exc):
                log.warning("report live aggregation: DB pool busy (%s)", ckey[0])
                raise _busy() from None
            if isinstance(exc, _BudgetExceeded) or _is_query_canceled(exc):
                log.error("report live aggregation timed out (%s)", ckey[0])
                await cache_set_failure(ckey, FAILURE_TTL)
                raise _unavailable(FAILURE_TTL) from None
            log.exception("report live aggregation failed (%s)", ckey[0])
            raise
        await cache_set(ckey, payload, fresh_secs=fresh, stale_secs=stale)
        await cache_clear_failure(ckey)
        return payload

    if single_flight:
        payload, _shared = await _single_flight(ckey, _compute_and_store, db=db)
        return payload
    return await _compute_and_store()


async def _limit_live_statement(db) -> None:
    await db.execute(text(f"SET LOCAL statement_timeout = '{LIVE_STATEMENT_TIMEOUT_MS}'"))


def _day_payload(rows, *, device_count: Optional[int] = None) -> list[dict]:
    """CompanyDayOut-shaped dicts, newest day first, from (day, active, idle,
    device_count) rows — cached or live, the shape is the same."""
    return [
        CompanyDayOut(
            date=str(r.day), active_seconds=int(r.active or 0), idle_seconds=int(r.idle or 0),
            device_count=int(r.device_count or 0) if device_count is None else device_count,
        ).model_dump()
        for r in sorted(rows, key=lambda r: r.day, reverse=True)
    ]


def _apps_payload(rows) -> list[dict]:
    return [AppUsageOut(app_name=r.app_name, seconds=int(r.seconds or 0)).model_dump() for r in rows]


@router.get("/company/summary", response_model=List[CompanyDayOut])
async def company_summary(
    days:  int = Query(default=7, ge=1, le=90),
    since: Optional[date] = Query(default=None, description="Start date YYYY-MM-DD (inclusive); requires `until`, mutually exclusive with a non-default `days`"),
    until: Optional[date] = Query(default=None, description="End date YYYY-MM-DD (inclusive); requires `since`"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    _validate_range(days, since, until)
    vids = await _scope_ids(admin, db)   # None = full scope
    r_since, r_until = _resolve_window(days, since, until)

    # Hoist scalars off the ORM object BEFORE any cache helper runs: a helper
    # error triggers db.rollback(), which expires `admin`, and a later
    # admin.company_id access would raise MissingGreenlet (500).
    cid = str(admin.company_id)
    _scope_fp = "all" if vids is None else str(admin.id)
    _ckey = ("report:summary", cid, _scope_fp, days, *_range_cache_fp(days, since, until))
    _redis_hit, _ = await cache_get(_ckey)
    if _redis_hit is not None:
        return _redis_hit

    # Fast path: the per-day cache tables (every range up to 2 years once the
    # worker's backfill horizon covers it). Full-scope admins read the company
    # summary, scoped users the per-device table filtered to their devices.
    if vids is None:
        cached = await report_cache.company_summary_range(db, cid, r_since, r_until)
    else:
        cached = await report_cache.device_summary_range(db, cid, vids, r_since, r_until)
    if cached is not None:
        _payload = _day_payload(cached)
        await cache_set(_ckey, _payload, fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
        return _payload

    # Fallback: live split + per-(day, device) pre-aggregate.
    async def _compute():
        await _limit_live_statement(db)
        return _day_payload(await live_daily_summary(db, cid, r_since, r_until, vids))

    return await _run_live(db, _ckey, _compute, fresh=FRESH_ANALYTICS, stale=STALE_ANALYTICS,
                           single_flight=True)


@router.get("/company/top-apps", response_model=List[AppUsageOut])
async def top_apps(
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    since: Optional[date] = Query(default=None, description="Start date YYYY-MM-DD (inclusive); requires `until`, mutually exclusive with a non-default `days`"),
    until: Optional[date] = Query(default=None, description="End date YYYY-MM-DD (inclusive); requires `since`"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    _validate_range(days, since, until)
    vids = await _scope_ids(admin, db)   # None = full scope
    r_since, r_until = _resolve_window(days, since, until)

    cid = str(admin.company_id)  # hoisted: see company_summary
    _scope_fp = "all" if vids is None else str(admin.id)
    _ckey = ("report:top_apps", cid, _scope_fp, days, limit, *_range_cache_fp(days, since, until))
    _redis_hit, _ = await cache_get(_ckey)
    if _redis_hit is not None:
        return _redis_hit

    # Fast path: full-scope admins sum report_daily_app_usage over the range.
    # Scoped users stay live (a handful of devices, covering index).
    if vids is None:
        cached = await report_cache.company_top_apps_range(db, cid, r_since, r_until, limit)
        if cached is not None:
            _payload = _apps_payload(cached)
            await cache_set(_ckey, _payload, fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return _payload

    async def _compute():
        await _limit_live_statement(db)
        return _apps_payload(await live_top_apps(db, cid, r_since, r_until, vids, limit))

    return await _run_live(db, _ckey, _compute, fresh=FRESH_ANALYTICS, stale=STALE_ANALYTICS,
                           single_flight=True)


@router.get("/company/top-urls", response_model=List[UrlVisitOut])
async def top_urls(
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    clause, sparams = await _scope_clause(admin, db)

    cid = str(admin.company_id)  # hoisted: see company_summary

    _scope_fp = "all" if not clause else str(admin.id)
    _ckey = ("report:top_urls", cid, _scope_fp, days, limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    result = await db.execute(
        text(f"""
            SELECT substring(ae.url FROM '^https?://([^/]+)') AS host, COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit' AND ae.url IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days){clause}
            GROUP BY host
            HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
            ORDER BY visits DESC LIMIT :limit
        """),
        {"company_id": cid, "days": days, "limit": limit, **sparams},
    )
    _items = [UrlVisitOut(url=r.host, visits=int(r.visits or 0)) for r in result.fetchall()]
    await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _items


@router.get("/device/{device_id}/top-apps", response_model=List[AppUsageOut])
async def device_top_apps(
    device_id: str,
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    since: Optional[date] = Query(default=None, description="Start date YYYY-MM-DD (inclusive); requires `until`, mutually exclusive with a non-default `days`"),
    until: Optional[date] = Query(default=None, description="End date YYYY-MM-DD (inclusive); requires `since`"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    _validate_range(days, since, until)
    device = await _get_visible_device(device_id, admin, db)
    window_clause, wparams = _window_clause(days, since, until)
    did = str(device.id)  # hoisted: a failure rolls the session back and expires `device`

    _ckey = ("report:dev_top_apps", did, days, limit, *_range_cache_fp(days, since, until))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    async def _compute():
        result = await db.execute(
            text(f"""
                SELECT normalize_app_name(ae.app_name) AS app_name,
                       SUM(ae.duration) AS seconds
                FROM activity_events ae
                WHERE ae.device_id = :device_id
                  AND ae.event_type = 'window_change'
                  AND ae.app_name IS NOT NULL
                  AND {window_clause}
                  {_EXCL_IDLE}
                GROUP BY normalize_app_name(ae.app_name) ORDER BY seconds DESC LIMIT :limit
            """),
            {"device_id": did, "limit": limit, **wparams},
        )
        return _apps_payload(result.fetchall())

    return await _run_live(db, _ckey, _compute, fresh=FRESH_DETAIL, stale=STALE_DETAIL)


@router.get("/device/{device_id}/summary", response_model=List[CompanyDayOut])
async def device_summary(
    device_id: str,
    days:  int = Query(default=7, ge=1, le=90),
    since: Optional[date] = Query(default=None, description="Start date YYYY-MM-DD (inclusive); requires `until`, mutually exclusive with a non-default `days`"),
    until: Optional[date] = Query(default=None, description="End date YYYY-MM-DD (inclusive); requires `since`"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    _validate_range(days, since, until)
    device = await _get_visible_device(device_id, admin, db)
    r_since, r_until = _resolve_window(days, since, until)
    did = str(device.id)             # hoisted: see device_top_apps
    cid = str(device.company_id)

    _ckey = ("report:dev_summary", did, days, *_range_cache_fp(days, since, until))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: this device's rows of the per-day device cache.
    cached = await report_cache.device_summary_range(db, cid, [did], r_since, r_until)
    if cached is not None:
        _payload = _day_payload(cached, device_count=1)
        await cache_set(_ckey, _payload, fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)
        return _payload

    async def _compute():
        return _day_payload(await live_daily_summary(db, cid, r_since, r_until, [did]), device_count=1)

    return await _run_live(db, _ckey, _compute, fresh=FRESH_DETAIL, stale=STALE_DETAIL)


# ── Fleet report schemas ───────────────────────────────────────────────────────

class PlatformCount(BaseModel):
    platform: str
    count: int

class TopDeviceOut(BaseModel):
    device_id: str
    hostname: str
    employee_name: Optional[str]
    active_seconds: int

class FleetReportOut(BaseModel):
    since: str
    until: str
    total_devices: int
    online_devices: int
    offline_devices: int
    archived_devices: int
    platform_breakdown: List[PlatformCount]
    daily_activity: List[CompanyDayOut]
    top_apps: List[AppUsageOut]
    most_active_devices: List[TopDeviceOut]


# ── Device full-report schemas ─────────────────────────────────────────────────

class DayBreakdownOut(BaseModel):
    date: str
    active_seconds: int
    idle_seconds: int

class AppDetailOut(BaseModel):
    app_name: str
    seconds: int
    pct: float

class DeviceInfoOut(BaseModel):
    id: str
    hostname: str
    platform: str
    employee_name: Optional[str]
    enrolled_at: str
    last_seen: Optional[str]

class DeviceFullReportOut(BaseModel):
    device: DeviceInfoOut
    since: str
    until: str
    total_active_seconds: int
    total_idle_seconds: int
    working_days: int
    daily_breakdown: List[DayBreakdownOut]
    top_apps: List[AppDetailOut]
    top_urls: List[UrlVisitOut]


# ── Device roster schemas (registered-device inventory, no activity metrics) ──

class DeviceRosterRowOut(BaseModel):
    hostname: str
    platform: str                    # normalized label: Windows / macOS / Linux / Other
    raw_platform: str                # as reported by the agent: win32 / darwin / …
    employee_name: Optional[str]
    status: str                      # online | offline | archived
    agent_version: Optional[str]
    enrolled_at: str
    last_seen: Optional[str]

class DeviceRosterReportOut(BaseModel):
    generated_at: str
    total_devices: int
    online_devices: int
    offline_devices: int
    archived_devices: int
    unassigned_devices: int
    platform_breakdown: List[PlatformCount]
    devices: List[DeviceRosterRowOut]


# ── Report job queue (Redis-backed background jobs) ───────────────────────────

_JOB_TTL = 1800  # 30 minutes
# Shown to the user when a job fails unexpectedly. str(exc) from a failed query
# carries the SQL text and bound parameters, so the real error goes to the log only.
_JOB_ERROR = "The report could not be generated. Please try again, or use a shorter date range."


class ReportJobRequest(BaseModel):
    type: str                          # "device_full" | "fleet" | "device_roster"
    device_id: Optional[str] = None
    since: Optional[str] = None        # YYYY-MM-DD — required except for device_roster
    until: Optional[str] = None        # YYYY-MM-DD (inclusive) — same


class ReportJobOut(BaseModel):
    job_id: str
    type: str
    status: str                 # pending | running | done | failed
    progress: int               # 0-100
    error: Optional[str] = None
    result: Optional[dict] = None


async def _job_get(job_id: str) -> dict | None:
    r = get_client()
    if not r:
        return None
    raw = await r.get(f"tt:rjob:{job_id}")
    return json.loads(raw) if raw else None


async def _job_set(job_id: str, data: dict) -> None:
    r = get_client()
    if r:
        await r.setex(f"tt:rjob:{job_id}", _JOB_TTL, json.dumps(data))


# ── Background workers ────────────────────────────────────────────────────────

async def _run_device_full_job(
    job_id: str, device_id: str, company_id: str, since: str, until: str,
) -> None:
    """Run a device_full report in the background; update job state in Redis throughout.

    The three activity queries run concurrently on separate sessions (the
    builder opens them itself when no `db` is passed)."""

    async def _upd(st: str, prog: int, error: str | None = None, result: dict | None = None):
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_full",
            "status": st, "progress": prog, "error": error, "result": result,
        })

    async def _progress(pct: int):
        await _upd("running", pct)

    try:
        since_dt = date.fromisoformat(since)
        until_dt = date.fromisoformat(until)

        result = await build_device_full_report(
            company_id, device_id, since_dt, until_dt, on_progress=_progress,
        )
        if result is None:
            await _upd("failed", 0, error="Device not found")
            return

        # Warm the regular GET cache so an immediate follow-up request is free
        _ckey = ("report:dev_full", device_id, str(since_dt), str(until_dt))
        await cache_set(_ckey, result, fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)

        await _upd("done", 100, result=result)

    except Exception:  # noqa: BLE001
        log.exception("report job failed")
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_full",
            "status": "failed", "progress": 0, "error": _JOB_ERROR, "result": None,
        })


async def _run_fleet_job(
    job_id: str, company_id: str, since: str, until: str, device_ids: list[str] | None,
) -> None:
    """Run a fleet report in the background, restricted to `device_ids`
    (None = company-wide; resolved from the caller's RBAC scope by the handler).

    All five queries run concurrently (separate sessions) so wall time ≈ slowest
    single query instead of the sum of all five.
    """

    async def _upd(st: str, prog: int, error: str | None = None, result: dict | None = None):
        await _job_set(job_id, {
            "job_id": job_id, "type": "fleet",
            "status": st, "progress": prog, "error": error, "result": result,
        })

    async def _progress(pct: int):
        await _upd("running", pct)

    try:
        since_dt = date.fromisoformat(since)
        until_dt = date.fromisoformat(until)

        _ckey = fleet_cache_key(company_id, since_dt, until_dt, device_ids)

        # Single-flight: identical concurrent jobs (PDF then Excel, two admins)
        # build the fleet once; the rest take the winner's cached result and
        # only compute themselves if it never shows up.
        async def _build_and_cache():
            built = await build_fleet_report(
                company_id, since_dt, until_dt, device_ids, on_progress=_progress,
            )
            # Cached BEFORE the lock is released, so a waiting job that sees the
            # lock vanish always finds the value.
            await cache_set(_ckey, built, fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return built

        result, _shared = await _single_flight(_ckey, _build_and_cache, honor_failure=False)
        await _upd("done", 100, result=result)

    except Exception:  # noqa: BLE001
        log.exception("report job failed")
        await _job_set(job_id, {
            "job_id": job_id, "type": "fleet",
            "status": "failed", "progress": 0, "error": _JOB_ERROR, "result": None,
        })


# ── Device roster worker ──────────────────────────────────────────────────────

# The agent reports sys.platform, so Windows arrives as 'win32' and macOS as
# 'darwin'. Anything not listed rolls up into "Other" rather than leaking a raw
# platform string into the report's grouping.
_PLATFORM_LABELS = {
    "win32": "Windows", "windows": "Windows", "cygwin": "Windows",
    "darwin": "macOS", "macos": "macOS",
    "linux": "Linux", "linux2": "Linux",
}


def _platform_label(raw: str | None) -> str:
    return _PLATFORM_LABELS.get((raw or "").strip().lower(), "Other")


async def _fetch_roster_rows(db: AsyncSession, company_id: str, device_ids: list[str] | None):
    """Every registered (non-deleted) device in scope, archived ones included.

    `device_ids=None` means company-wide (scope 'all'); a list restricts to the
    caller's visible devices as resolved by app/core/scoping.py.
    """
    scope = "" if device_ids is None else " AND d.id = ANY(:vids)"
    params: dict = {"cid": company_id, "cutoff": ONLINE_CUTOFF_SECS}
    if device_ids is not None:
        params["vids"] = device_ids

    return (await db.execute(text(f"""
        SELECT d.hostname,
               d.platform,
               d.agent_version,
               d.enrolled_at,
               d.last_seen,
               d.is_archived,
               COALESCE(
                   d.is_archived = FALSE
                   AND d.last_seen >= NOW() - (:cutoff * INTERVAL '1 second'),
                   FALSE
               ) AS is_online,
               e.name AS employee_name
        FROM   devices d
        LEFT JOIN employees e ON e.id = d.employee_id
        WHERE  d.company_id = :cid
          AND  d.is_deleted = FALSE{scope}
        ORDER  BY d.is_archived, e.name NULLS LAST, d.hostname
    """), params)).fetchall()


def _roster_result(rows) -> dict:
    """Shape roster rows into the report payload: inventory + counts, no activity."""
    devices: list[dict] = []
    platform_counts: dict[str, int] = {}
    online = offline = archived = unassigned = 0

    for r in rows:
        label = _platform_label(r.platform)
        platform_counts[label] = platform_counts.get(label, 0) + 1

        if r.is_archived:
            row_status = "archived"
            archived += 1
        elif r.is_online:
            row_status = "online"
            online += 1
        else:
            row_status = "offline"
            offline += 1

        if not r.employee_name:
            unassigned += 1

        devices.append({
            "hostname":      r.hostname,
            "platform":      label,
            "raw_platform":  r.platform or "",
            "employee_name": r.employee_name,
            "status":        row_status,
            "agent_version": r.agent_version,
            "enrolled_at":   r.enrolled_at.isoformat() if r.enrolled_at else "",
            "last_seen":     r.last_seen.isoformat() if r.last_seen else None,
        })

    return {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "total_devices":      len(devices),
        "online_devices":     online,
        "offline_devices":    offline,
        "archived_devices":   archived,
        "unassigned_devices": unassigned,
        "platform_breakdown": [
            {"platform": p, "count": c}
            for p, c in sorted(platform_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "devices": devices,
    }


async def _run_device_roster_job(
    job_id: str, company_id: str, device_ids: list[str] | None,
) -> None:
    """Registered-device inventory snapshot. One cheap query — no activity_events."""

    async def _upd(st: str, prog: int, error: str | None = None, result: dict | None = None):
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_roster",
            "status": st, "progress": prog, "error": error, "result": result,
        })

    try:
        await _upd("running", 10)
        async with report_cache.background_session() as db:
            rows = await _fetch_roster_rows(db, company_id, device_ids)
        await _upd("done", 100, result=_roster_result(rows))

    except Exception:  # noqa: BLE001
        log.exception("report job failed")
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_roster",
            "status": "failed", "progress": 0, "error": _JOB_ERROR, "result": None,
        })



# ── Job endpoints ─────────────────────────────────────────────────────────────

@router.post("/jobs", response_model=ReportJobOut, status_code=202)
async def create_report_job(
    req: ReportJobRequest,
    background_tasks: BackgroundTasks,
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Queue a report job. Returns job_id immediately; poll GET /jobs/{id} for progress.
    Falls back to running synchronously when Redis is unavailable — the response will
    contain status='done' and the full result already populated."""
    if req.type not in ("device_full", "fleet", "device_roster"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type must be 'device_full', 'fleet' or 'device_roster'",
        )

    cid = str(admin.company_id)

    # device_roster is a point-in-time inventory snapshot — it takes no date range.
    since_dt = until_dt = None
    if req.type != "device_roster":
        since_dt, until_dt = _validate_job_range(req.since, req.until)

    device_id: str | None = None
    if req.type == "device_full":
        if not req.device_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="device_id required for device_full")
        device = await _get_visible_device(req.device_id, admin, db)
        device_id = str(device.id)

    # Resolve the caller's device visibility here, while the request session and
    # `admin` are alive — the background worker has neither. Fleet and roster
    # are the two company-wide report types, so both are scoped this way.
    scope_ids: list[str] | None = None
    if req.type in ("device_roster", "fleet"):
        ids = await visible_device_ids(admin, db)
        scope_ids = None if ids is ALL else [str(i) for i in ids]

    job_id = str(uuid.uuid4())

    # ── Redis available: true background job ──────────────────────────────────
    if get_client():
        # The workers warm the same cache the GET endpoints read, so a repeat
        # export of an identical request (e.g. PDF then Excel) is served from it
        # instead of re-scanning the range.
        cached = None
        if req.type == "device_full":
            cached, _ = await cache_get(("report:dev_full", device_id, str(since_dt), str(until_dt)))
        elif req.type == "fleet":
            cached, _ = await cache_get(fleet_cache_key(cid, since_dt, until_dt, scope_ids))
        if cached is not None:
            done: dict = {
                "job_id": job_id, "type": req.type,
                "status": "done", "progress": 100, "error": None, "result": cached,
            }
            await _job_set(job_id, done)
            return done

        initial: dict = {
            "job_id": job_id, "type": req.type,
            "status": "pending", "progress": 0, "error": None, "result": None,
        }
        await _job_set(job_id, initial)
        if req.type == "device_full":
            background_tasks.add_task(_run_device_full_job, job_id, device_id, cid, str(since_dt), str(until_dt))
        elif req.type == "device_roster":
            background_tasks.add_task(_run_device_roster_job, job_id, cid, scope_ids)
        else:
            background_tasks.add_task(_run_fleet_job, job_id, cid, str(since_dt), str(until_dt), scope_ids)
        return initial

    # ── Redis unavailable: run synchronously, return completed job inline ─────
    # Same builders as the workers, but sequential on the request's db session
    # (one AsyncSession cannot run concurrent queries).
    try:
        if req.type == "device_roster":
            result = _roster_result(await _fetch_roster_rows(db, cid, scope_ids))

        elif req.type == "device_full":
            result = await build_device_full_report(
                cid, device_id, since_dt, until_dt, db=db, timeout_ms=LIVE_STATEMENT_TIMEOUT_MS)
            if result is None:
                return {
                    "job_id": job_id, "type": req.type,
                    "status": "failed", "progress": 0, "error": "Device not found", "result": None,
                }

        else:  # fleet
            result = await build_fleet_report(
                cid, since_dt, until_dt, scope_ids, db=db, timeout_ms=LIVE_STATEMENT_TIMEOUT_MS)

        return {
            "job_id": job_id, "type": req.type,
            "status": "done", "progress": 100, "error": None, "result": result,
        }

    except Exception:  # noqa: BLE001
        log.exception("report job failed")
        return {
            "job_id": job_id, "type": req.type,
            "status": "failed", "progress": 0, "error": _JOB_ERROR, "result": None,
        }


@router.get("/jobs/{job_id}", response_model=ReportJobOut)
async def get_report_job(
    job_id: str,
    _admin: Employee = Depends(require("reports", "view")),
):
    """Poll report job status. result is populated when status='done'."""
    job = await _job_get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found or expired")
    return job


# ── Fleet report endpoint ─────────────────────────────────────────────────────

@router.get("/devices/fleet", response_model=FleetReportOut)
async def fleet_report(
    since: str = Query(..., description="Start date YYYY-MM-DD"),
    until: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Board-level fleet report: counts, platform split, activity trend, top apps.
    Scoped to the caller's visible devices."""
    since_dt, until_dt = _validate_job_range(since, until)
    cid = str(admin.company_id)

    ids = await visible_device_ids(admin, db)
    scope_ids = None if ids is ALL else [str(i) for i in ids]

    _ckey = fleet_cache_key(cid, since_dt, until_dt, scope_ids)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    async def _compute():
        return await build_fleet_report(
            cid, since_dt, until_dt, scope_ids, db=db, timeout_ms=LIVE_STATEMENT_TIMEOUT_MS)

    return await _run_live(db, _ckey, _compute, fresh=FRESH_ANALYTICS, stale=STALE_ANALYTICS,
                           single_flight=True)


# ── Device full report endpoint ───────────────────────────────────────────────

@router.get("/device/{device_id}/full", response_model=DeviceFullReportOut)
async def device_full_report(
    device_id: str,
    since: str = Query(..., description="Start date YYYY-MM-DD"),
    until: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Full per-device report: day-wise breakdown, app usage, and URL visits."""
    since_dt, until_dt = _validate_job_range(since, until)
    device = await _get_visible_device(device_id, admin, db)

    did = str(device.id)
    cid = str(device.company_id)
    _ckey = ("report:dev_full", did, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    async def _compute():
        result = await build_device_full_report(
            cid, did, since_dt, until_dt, db=db, timeout_ms=LIVE_STATEMENT_TIMEOUT_MS)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
        return result

    return await _run_live(db, _ckey, _compute, fresh=FRESH_DETAIL, stale=STALE_DETAIL)
