"""Per-day report cache: range readers, coverage gating, backfill, failure
markers and single-flight.

Covers:
- _compute_day fills report_daily_summary / report_daily_device_cache /
  report_daily_app_usage, and the range readers (company summary, device-set
  summary, company top apps) return EXACTLY what the live queries return for the
  same seeded data — including the device_count definition (a device counts for a
  day when it logged a window_change with an app_name or an idle_start).
- Recomputing a day is idempotent.
- covers(): no state row, a range older than covered_since, and a stale
  last_fast_refresh on a range that includes today each make the readers return
  None (caller goes live).
- The backfill step lowers covered_since one day at a time, computes that day,
  and stops at COVER_DAYS.
- The fleet / device_full builders give the same numbers from the cache as live.
- A failing live aggregation answers 503 + Retry-After, and while the failure
  marker lives the identical request never reaches the database; validation
  errors are never marked.
- Single-flight: N concurrent identical live requests run the builder once.

Seeded days are in 2020, long before any real activity, so computing "a day for
every company" on the shared dev database touches no real rows, and everything
runs in the never-committed session fixture.
"""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.core import RedisCache, report_cache
from app.db.models import ActivityEvent, Company, Device, Employee, Role, RolePermission
from app.services import ReportQueries
from app.services.ReportQueries import (
    build_device_full_report,
    build_fleet_report,
    live_daily_summary,
    live_top_apps,
)

DAY = date(2020, 3, 15)


# ── Fixture helpers ────────────────────────────────────────────────────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"RC-{tag}", enrollment_token=f"tok-{tag}")
    db.add(c)
    await db.flush()
    return c


async def _device(db, company, hostname):
    d = Device(
        company_id=company.id, hostname=f"{hostname}-{uuid.uuid4().hex[:4]}", platform="macos",
        is_active=True, last_seen=datetime.now(timezone.utc),
    )
    db.add(d)
    await db.flush()
    return d


def _noon(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)


async def _event(db, device, *, app_name, duration, day=DAY, event_type="window_change"):
    db.add(ActivityEvent(
        device_id=device.id, event_type=event_type, app_name=app_name,
        window_title=app_name, duration=duration, timestamp=_noon(day),
    ))
    await db.flush()


async def _set_state(db, covered_since, *, age_minutes=0):
    await db.execute(text("""
        INSERT INTO report_cache_state (name, covered_since, last_fast_refresh)
        VALUES ('global', CAST(:cs AS date), NOW() - (CAST(:age AS integer) * INTERVAL '1 minute'))
        ON CONFLICT (name) DO UPDATE SET
            covered_since = EXCLUDED.covered_since,
            last_fast_refresh = EXCLUDED.last_fast_refresh
    """), {"cs": covered_since, "age": age_minutes})


async def _seed(db, day=DAY):
    """One company, five devices on `day` (default DAY):
      A  Xcode 100, raw 'chrome' 50 (-> Google Chrome), loginwindow 500 (idle
         app: no active time, no app row), idle_start 30
      B  Xcode 70
      C  idle_start 20 only                       (counts as a device that day)
      D  url_visit only                           (does NOT count)
      E  window_change with app_name NULL         (does NOT count)
    """
    company = await _company(db)
    a, b, c, d, e = [await _device(db, company, n) for n in "abcde"]
    await _event(db, a, app_name="Xcode", duration=100, day=day)
    await _event(db, a, app_name="chrome", duration=50, day=day)
    await _event(db, a, app_name="loginwindow", duration=500, day=day)
    await _event(db, a, app_name=None, duration=30, event_type="idle_start", day=day)
    await _event(db, b, app_name="Xcode", duration=70, day=day)
    await _event(db, c, app_name=None, duration=20, event_type="idle_start", day=day)
    await _event(db, d, app_name=None, duration=10, event_type="url_visit", day=day)
    await _event(db, e, app_name=None, duration=40, day=day)
    return company, a, b, c, d, e


def _norm_daily(rows):
    return [(str(r.day), int(r.active or 0), int(r.idle or 0), int(r.device_count or 0))
            for r in sorted(rows, key=lambda r: r.day)]


def _norm_apps(rows):
    return [(r.app_name, int(r.seconds or 0)) for r in rows]


# ── Readers equal the live queries ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_readers_equal_live_queries(db_session):
    company, a, b, c, *_ = await _seed(db_session)
    cid = str(company.id)
    await report_cache._compute_day(db_session, DAY)
    await _set_state(db_session, DAY)

    # Company summary
    cached = await report_cache.company_summary_range(db_session, cid, DAY, DAY)
    live = await live_daily_summary(db_session, cid, DAY, DAY, None)
    assert _norm_daily(cached) == _norm_daily(live) == [(str(DAY), 220, 50, 3)]

    # Device-set summary (A and B only: C's idle device is out of scope)
    ids = [str(a.id), str(b.id)]
    cached = await report_cache.device_summary_range(db_session, cid, ids, DAY, DAY)
    live = await live_daily_summary(db_session, cid, DAY, DAY, ids)
    assert _norm_daily(cached) == _norm_daily(live) == [(str(DAY), 220, 30, 2)]

    # A single device
    cached = await report_cache.device_summary_range(db_session, cid, [str(c.id)], DAY, DAY)
    live = await live_daily_summary(db_session, cid, DAY, DAY, [str(c.id)])
    assert _norm_daily(cached) == _norm_daily(live) == [(str(DAY), 0, 20, 1)]

    # Top apps: normalized names, idle/lock-screen apps excluded, same order
    cached = await report_cache.company_top_apps_range(db_session, cid, DAY, DAY, 20)
    live = await live_top_apps(db_session, cid, DAY, DAY, None, 20)
    assert _norm_apps(cached) == _norm_apps(live) == [("Xcode", 170), ("Google Chrome", 50)]

    # Quiet (settled) range inside the horizon: an empty list is a real answer,
    # not a miss that sends a quiet company to a company-wide live scan.
    other = DAY - timedelta(days=1)
    await _set_state(db_session, other)
    assert await report_cache.company_summary_range(db_session, cid, other, other) == []
    assert await report_cache.company_top_apps_range(db_session, cid, other, other, 5) == []
    await db_session.rollback()


@pytest.mark.asyncio
async def test_live_device_count_is_window_or_idle_devices(db_session):
    """The live summary counts a device only if it logged a window_change with an
    app_name or an idle_start that day — url_visit-only and NULL-app window events
    don't count (the cache stores exactly this definition)."""
    company, *_ = await _seed(db_session)
    live = await live_daily_summary(db_session, str(company.id), DAY, DAY, None)
    assert [(int(r.device_count), int(r.active), int(r.idle)) for r in live] == [(3, 220, 50)]
    await db_session.rollback()


@pytest.mark.asyncio
async def test_compute_day_is_idempotent(db_session):
    company, *_ = await _seed(db_session)
    cid = str(company.id)

    async def _snapshot():
        out = {}
        for tbl, cols in (
            ("report_daily_summary", "day, active_seconds, idle_seconds, device_count"),
            ("report_daily_device_cache", "device_id::text, active_seconds, idle_seconds"),
            ("report_daily_app_usage", "app_name, seconds"),
        ):
            rows = (await db_session.execute(
                text(f"SELECT {cols} FROM {tbl} WHERE company_id = :c ORDER BY 1, 2"), {"c": cid}
            )).fetchall()
            out[tbl] = [tuple(r) for r in rows]
        return out

    await report_cache._compute_day(db_session, DAY)
    first = await _snapshot()
    await report_cache._compute_day(db_session, DAY)
    assert await _snapshot() == first
    assert len(first["report_daily_summary"]) == 1
    assert len(first["report_daily_device_cache"]) == 3   # A, B, C
    assert len(first["report_daily_app_usage"]) == 2      # Xcode, Google Chrome
    await db_session.rollback()


# ── Coverage gating ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_readers_return_none_outside_the_horizon_or_without_state(db_session):
    company, a, *_ = await _seed(db_session)
    cid = str(company.id)
    await report_cache._compute_day(db_session, DAY)

    # No state row at all: nothing is covered.
    await db_session.execute(text("DELETE FROM report_cache_state"))
    assert await report_cache.company_summary_range(db_session, cid, DAY, DAY) is None
    assert await report_cache.device_summary_range(db_session, cid, [str(a.id)], DAY, DAY) is None
    assert await report_cache.company_top_apps_range(db_session, cid, DAY, DAY, 5) is None

    # Horizon starts the day AFTER the range: still a miss.
    await _set_state(db_session, DAY + timedelta(days=1))
    assert await report_cache.company_summary_range(db_session, cid, DAY, DAY) is None
    assert await report_cache.company_top_apps_range(db_session, cid, DAY, DAY, 5) is None
    # A range that merely STARTS before the horizon is a miss too, never partial.
    assert await report_cache.company_summary_range(db_session, cid, DAY, DAY + timedelta(days=3)) is None

    # Horizon at the range start: hit.
    await _set_state(db_session, DAY)
    assert await report_cache.company_summary_range(db_session, cid, DAY, DAY) is not None
    await db_session.rollback()


@pytest.mark.asyncio
async def test_staleness_guard_only_applies_to_ranges_touching_today(db_session):
    today = datetime.now(timezone.utc).date()
    await _set_state(db_session, DAY, age_minutes=report_cache.STALE_MINUTES_DAILY + 5)
    # Stale fast cycle: today/yesterday can't be trusted ...
    assert await report_cache.covers(db_session, today - timedelta(days=5), today) is False
    assert await report_cache.covers(db_session, today - timedelta(days=5), today - timedelta(days=1)) is False
    # ... but settled days are final no matter how old last_fast_refresh is.
    assert await report_cache.covers(db_session, today - timedelta(days=30), today - timedelta(days=2)) is True

    await _set_state(db_session, DAY, age_minutes=1)
    assert await report_cache.covers(db_session, today - timedelta(days=5), today) is True
    await db_session.rollback()


# ── Backfill ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_step_extends_horizon_one_day_and_stops_at_target(db_session):
    today = datetime.now(timezone.utc).date()
    day = today - timedelta(days=600)      # inside the 732-day target, before any real data
    company, *_ = await _seed(db_session, day)
    cid = str(company.id)

    # No state yet: nothing to extend.
    await db_session.execute(text("DELETE FROM report_cache_state"))
    assert await report_cache._extend_horizon_one_day(db_session, today) is None

    # Horizon sits just above `day`: one step computes it and lowers the horizon.
    await _set_state(db_session, day + timedelta(days=1))
    assert await report_cache._extend_horizon_one_day(db_session, today) == day
    cs = (await db_session.execute(text("SELECT covered_since FROM report_cache_state"))).scalar()
    assert cs == day
    cached = await report_cache.company_summary_range(db_session, cid, day, day)
    assert _norm_daily(cached) == [(str(day), 220, 50, 3)]

    # Next step goes one further back (an empty day is still "covered").
    assert await report_cache._extend_horizon_one_day(db_session, today) == day - timedelta(days=1)
    cs = (await db_session.execute(text("SELECT covered_since FROM report_cache_state"))).scalar()
    assert cs == day - timedelta(days=1)

    # At the target horizon the backfill is done.
    await _set_state(db_session, today - timedelta(days=report_cache.COVER_DAYS))
    assert await report_cache._extend_horizon_one_day(db_session, today) is None
    assert report_cache.COVER_DAYS >= 732   # MAX_REPORT_RANGE_DAYS
    await db_session.rollback()


# ── Exports read the same caches ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fleet_and_device_full_from_cache_equal_live(db_session, monkeypatch):
    company, a, b, *_ = await _seed(db_session)
    cid = str(company.id)
    await report_cache._compute_day(db_session, DAY)
    await _set_state(db_session, DAY)

    async def _never(db, since, until):
        return False

    for scope in (None, [str(a.id), str(b.id)], [str(a.id)]):
        cached = await build_fleet_report(cid, DAY, DAY, scope, db=db_session)
        with monkeypatch.context() as m:
            m.setattr(report_cache, "covers", _never)
            live = await build_fleet_report(cid, DAY, DAY, scope, db=db_session)
        for key in ("daily_activity", "top_apps", "most_active_devices"):
            assert cached[key] == live[key], (scope, key)

    full_cached = await build_device_full_report(cid, str(a.id), DAY, DAY, db=db_session)
    with monkeypatch.context() as m:
        m.setattr(report_cache, "covers", _never)
        full_live = await build_device_full_report(cid, str(a.id), DAY, DAY, db=db_session)
    assert full_cached["daily_breakdown"] == full_live["daily_breakdown"] == [
        {"date": str(DAY), "active_seconds": 150, "idle_seconds": 30}]
    assert full_cached["total_active_seconds"] == 150
    await db_session.rollback()


# ── Negative caching of failures + single-flight (fake Redis) ──────────────────

class _FakeRedis:
    """Just enough of redis.asyncio for RedisCache: get/set(nx, ex)/setex/delete/
    exists/ttl, with real expiry so TTL assertions mean something."""

    def __init__(self):
        self.store: dict[str, tuple[bytes, float | None]] = {}

    def _live(self, key):
        item = self.store.get(key)
        if item is None:
            return None
        if item[1] is not None and item[1] <= asyncio.get_event_loop().time():
            del self.store[key]
            return None
        return item

    async def get(self, key):
        item = self._live(key)
        return item[0] if item else None

    async def set(self, key, value, nx=False, ex=None):
        if nx and self._live(key) is not None:
            return None
        self.store[key] = (value, asyncio.get_event_loop().time() + ex if ex else None)
        return True

    async def setex(self, key, ttl, value):
        self.store[key] = (value, asyncio.get_event_loop().time() + ttl)
        return True

    async def delete(self, key):
        return 1 if self.store.pop(key, None) else 0

    async def eval(self, script, numkeys, key, token):
        """The one script RedisCache uses: delete `key` only if it holds `token`."""
        item = self._live(key)
        if item is not None and item[0] == token:
            del self.store[key]
            return 1
        return 0

    async def exists(self, key):
        return 1 if self._live(key) else 0

    async def ttl(self, key):
        item = self._live(key)
        if item is None:
            return -2
        if item[1] is None:
            return -1
        return max(int(item[1] - asyncio.get_event_loop().time() + 0.999), 1)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(RedisCache, "get_client", lambda: r)
    return r


def _query_canceled():
    """What a statement_timeout looks like coming out of asyncpg."""
    from asyncpg.exceptions import QueryCanceledError
    return QueryCanceledError("canceling statement due to statement timeout")


class _Db:
    """Stand-in for the request session: only rollback() matters to _run_live."""
    def __init__(self):
        self.rollback = AsyncMock()


@pytest.mark.asyncio
async def test_failure_marker_roundtrip(fake_redis):
    key = ("report:summary", "c1", "all", 7, "2026-01-01", "2026-01-07")
    assert await RedisCache.cache_get_failure(key) is None
    await RedisCache.cache_set_failure(key, ttl=30)
    remaining = await RedisCache.cache_get_failure(key)
    assert remaining is not None and 1 <= remaining <= 30
    # An identical key is marked, a different one is not.
    assert await RedisCache.cache_get_failure(("report:summary", "c1", "all", 7, "2026-01-01", "2026-01-08")) is None
    await RedisCache.cache_clear_failure(key)
    assert await RedisCache.cache_get_failure(key) is None


@pytest.mark.asyncio
async def test_run_live_failure_rolls_back_marks_and_503s(fake_redis):
    from fastapi import HTTPException
    from app.api.v1.endpoints import reports

    key = ("report:summary", "c1", "all", 7, "2026-01-01", "2026-01-07")
    db = _Db()
    calls = 0

    async def _boom():
        nonlocal calls
        calls += 1
        raise _query_canceled()

    with pytest.raises(HTTPException) as e1:
        await reports._run_live(db, key, _boom, fresh=60, stale=120)
    assert e1.value.status_code == 503
    assert e1.value.headers["Retry-After"] == str(RedisCache.FAILURE_TTL)
    assert "taking too long" in e1.value.detail and "retry in" in e1.value.detail
    db.rollback.assert_awaited()
    assert calls == 1

    # While the marker lives, the identical request is refused WITHOUT running anything.
    with pytest.raises(HTTPException) as e2:
        await reports._run_live(db, key, _boom, fresh=60, stale=120)
    assert e2.value.status_code == 503
    assert 1 <= int(e2.value.headers["Retry-After"]) <= RedisCache.FAILURE_TTL
    assert calls == 1

    # A different cache key is unaffected.
    other = key[:-1] + ("2026-01-09",)

    async def _ok():
        return [{"date": "2026-01-09"}]

    assert await reports._run_live(db, other, _ok, fresh=60, stale=120) == [{"date": "2026-01-09"}]


@pytest.mark.asyncio
async def test_endpoint_503_with_retry_after_and_no_db_on_repeat(client, db_session, fake_redis, monkeypatch):
    """End to end: a failing live aggregation answers 503 + Retry-After, the
    session is rolled back, and while the marker lives the identical request
    never reaches the database. Validation errors are never marked."""
    from app.api.v1.endpoints import reports

    async def _never(db, since, until):
        return False

    monkeypatch.setattr(report_cache, "covers", _never)

    company = await _company(db_session)
    role = Role(company_id=company.id, name="Owner", scope_type="all", is_system=True)
    db_session.add(role)
    await db_session.flush()
    db_session.add(RolePermission(role_id=role.id, module="reports", action="view"))
    tag = uuid.uuid4().hex[:6]
    admin = Employee(company_id=company.id, email=f"o-{tag}@test.io", name="Owner", role_id=role.id,
                     is_active=True, is_approved=True, password_hash="x")
    db_session.add(admin)
    await db_session.flush()
    from app.core.security import create_admin_token
    headers = {"Authorization": f"Bearer {create_admin_token(str(admin.id), admin.role, str(company.id))}"}

    calls = 0

    async def _boom(*a, **k):
        nonlocal calls
        calls += 1
        raise _query_canceled()

    monkeypatch.setattr(reports, "live_daily_summary", _boom)
    # The shared test session also holds the seeded rows: keep them across the
    # failure-path rollback, but still prove it was requested.
    rollback = AsyncMock()
    monkeypatch.setattr(db_session, "rollback", rollback)

    params = {"since": "2026-01-01", "until": "2026-01-31"}
    r1 = await client.get("/v1/reports/company/summary", params=params, headers=headers)
    assert r1.status_code == 503
    assert int(r1.headers["Retry-After"]) >= 1
    assert "taking too long" in r1.json()["detail"]
    rollback.assert_awaited()
    assert calls == 1

    r2 = await client.get("/v1/reports/company/summary", params=params, headers=headers)
    assert r2.status_code == 503 and calls == 1, "marked request must not touch the DB again"

    # Another range is a different key and runs (and fails) on its own.
    r3 = await client.get("/v1/reports/company/summary",
                          params={"since": "2026-02-01", "until": "2026-02-28"}, headers=headers)
    assert r3.status_code == 503 and calls == 2

    # 4xx validation errors never create a marker.
    bad = {"since": "2026-03-10", "until": "2026-03-01"}
    assert (await client.get("/v1/reports/company/summary", params=bad, headers=headers)).status_code == 400
    assert not [k for k in fake_redis.store if "fail:" in k and "2026-03" in k]


@pytest.mark.asyncio
async def test_single_flight_runs_the_builder_once(fake_redis):
    from app.api.v1.endpoints import reports

    key = ("report:fleet", "c1", "all", "2026-01-01", "2026-01-31")
    calls = 0

    async def _builder():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.3)
        return {"daily_activity": [], "n": calls}

    results = await asyncio.gather(*[
        reports._run_live(_Db(), key, _builder, fresh=60, stale=120, single_flight=True)
        for _ in range(6)
    ])
    assert calls == 1
    assert all(r == {"daily_activity": [], "n": 1} for r in results)
    # The lock is released and the value cached for later callers.
    assert not [k for k in fake_redis.store if ":lock:" in k]
    assert (await RedisCache.cache_get(key))[0] == {"daily_activity": [], "n": 1}


@pytest.mark.asyncio
async def test_single_flight_loser_computes_if_winner_never_delivers(fake_redis, monkeypatch):
    """The lock is an optimisation, never a gate: if the holder dies without
    caching a value, the loser times out waiting and computes for itself."""
    from app.api.v1.endpoints import reports

    key = ("report:fleet", "c2", "all", "2026-01-01", "2026-01-31")
    assert await RedisCache.try_acquire(key, ttl=30)      # a "winner" that will never deliver
    monkeypatch.setattr(reports, "_LIVE_WAIT_SECS", 0.2)

    async def _builder():
        return {"ok": True}

    out = await reports._run_live(_Db(), key, _builder, fresh=60, stale=120, single_flight=True)
    assert out == {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Review-finding fixes: failure classification, request-path budget, single-flight
# take-over / token release, late-data recompute, home freshness, background pool
# ═══════════════════════════════════════════════════════════════════════════════

KEY = ("report:summary", "c9", "all", 7, "2026-01-01", "2026-01-07")


def _marked(fake_redis) -> bool:
    return any("fail:" in k for k in fake_redis.store)


# ── M4: only statement timeouts write the failure marker ──────────────────────

@pytest.mark.asyncio
async def test_marker_written_for_wrapped_and_sqlstate_query_cancellation(fake_redis):
    """asyncpg QueryCanceledError wrapped by SQLAlchemy (.orig / __cause__) and
    anything carrying sqlstate 57014 are statement timeouts."""
    from fastapi import HTTPException
    from sqlalchemy.exc import DBAPIError, OperationalError
    from app.api.v1.endpoints import reports

    class _Sqlstate57014(Exception):
        sqlstate = "57014"

    def _via_cause():
        wrapped = Exception("adapted")
        wrapped.__cause__ = _query_canceled()
        return OperationalError("SELECT 1", {}, wrapped)

    for n, make in enumerate((
        lambda: OperationalError("SELECT 1", {}, _query_canceled()),
        lambda: DBAPIError("SELECT 1", {}, _query_canceled()),
        _via_cause,
        _Sqlstate57014,
    )):
        key = KEY[:-1] + (f"2026-02-0{n}",)
        db = _Db()

        async def _boom(make=make):
            raise make()

        with pytest.raises(HTTPException) as e:
            await reports._run_live(db, key, _boom, fresh=60, stale=120)
        assert e.value.status_code == 503, n
        assert await RedisCache.cache_get_failure(key) is not None, n


@pytest.mark.asyncio
async def test_other_errors_roll_back_propagate_and_write_no_marker(fake_redis):
    from sqlalchemy.exc import OperationalError
    from app.api.v1.endpoints import reports

    for n, exc in enumerate((
        RuntimeError("code bug"),
        OperationalError("SELECT 1", {}, ConnectionResetError("connection lost")),
    )):
        key = KEY[:-1] + (f"2026-03-0{n}",)
        db = _Db()

        async def _boom(exc=exc):
            raise exc

        with pytest.raises(type(exc)):          # a normal 500, not a 503
            await reports._run_live(db, key, _boom, fresh=60, stale=120, single_flight=True)
        db.rollback.assert_awaited()
        assert not _marked(fake_redis)
        assert not [k for k in fake_redis.store if ":lock:" in k], "lock must be released"


@pytest.mark.asyncio
async def test_pool_checkout_timeout_is_503_retry_after_5_and_no_marker(fake_redis):
    from fastapi import HTTPException
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from app.api.v1.endpoints import reports

    db = _Db()

    async def _pool_busy():
        raise PoolTimeout("QueuePool limit of size 3 overflow 2 reached, connection timed out")

    with pytest.raises(HTTPException) as e:
        await reports._run_live(db, KEY, _pool_busy, fresh=60, stale=120, single_flight=True)
    assert e.value.status_code == 503
    assert e.value.headers["Retry-After"] == "5"
    assert not _marked(fake_redis), "a busy pool must not lock every admin out for 30 s"
    assert not [k for k in fake_redis.store if ":lock:" in k]
    # ... so the very next identical request is free to run.
    async def _ok():
        return [{"ok": 1}]
    assert await reports._run_live(db, KEY, _ok, fresh=60, stale=120) == [{"ok": 1}]


@pytest.mark.asyncio
async def test_total_budget_overrun_is_marked_and_cancels_the_compute(fake_redis, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1.endpoints import reports

    monkeypatch.setattr(reports, "_LIVE_COMPUTE_BUDGET_SECS", 0.1)
    cancelled = []

    async def _slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    db = _Db()
    with pytest.raises(HTTPException) as e:
        await reports._run_live(db, KEY, _slow, fresh=60, stale=120, single_flight=True)
    assert e.value.status_code == 503 and cancelled
    assert await RedisCache.cache_get_failure(KEY) is not None
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_budget_cancel_leaves_the_real_session_usable(db_session):
    """_bounded cancels a real in-flight pg statement; the session must survive a
    rollback and serve the next query (what _run_live does right after)."""
    from app.api.v1.endpoints import reports

    async def _sleepy():
        await db_session.execute(text("SELECT pg_sleep(10)"))

    with pytest.raises(reports._BudgetExceeded):
        await reports._bounded(_sleepy(), 0.3)
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT 41 + 1"))).scalar() == 42


def test_lock_ttl_outlives_every_request_path_budget():
    from app.api.v1.endpoints import reports
    assert reports._LIVE_LOCK_TTL > reports._LIVE_COMPUTE_BUDGET_SECS
    assert reports._LIVE_COMPUTE_BUDGET_SECS > ReportQueries.LIVE_STATEMENT_TIMEOUT_MS / 1000
    assert reports.LIVE_STATEMENT_TIMEOUT_MS == ReportQueries.LIVE_STATEMENT_TIMEOUT_MS == 60_000


# ── M5: request path = 60 s statements, background jobs = 300 s ───────────────

class _SpyDb:
    """Delegates to the real session, remembering every SET LOCAL statement_timeout."""
    def __init__(self, real):
        self.real, self.timeouts = real, []

    async def execute(self, stmt, *a, **k):
        sql = str(stmt)
        if "statement_timeout" in sql:
            self.timeouts.append(sql.split("'")[1])
        return await self.real.execute(stmt, *a, **k)

    async def rollback(self):
        await self.real.rollback()


@pytest.mark.asyncio
async def test_request_path_builders_use_the_60s_live_timeout(db_session):
    company, a, *_ = await _seed(db_session)
    cid = str(company.id)
    await report_cache._compute_day(db_session, DAY)
    await _set_state(db_session, DAY)

    spy = _SpyDb(db_session)
    await build_fleet_report(cid, DAY, DAY, None, db=spy)
    await build_device_full_report(cid, str(a.id), DAY, DAY, db=spy)
    assert spy.timeouts and set(spy.timeouts) == {"60000"}   # never the 300 s export budget

    spy = _SpyDb(db_session)
    await build_fleet_report(cid, DAY, DAY, None, db=spy, timeout_ms=ReportQueries.LIVE_STATEMENT_TIMEOUT_MS)
    assert set(spy.timeouts) == {"60000"}
    await db_session.rollback()


@pytest.mark.asyncio
async def test_background_jobs_keep_300s_and_use_the_background_pool(monkeypatch):
    """db=None (export workers): every private session comes from
    report_cache.background_session (BackgroundSessionLocal) with 300 s."""
    from contextlib import asynccontextmanager

    seen = []

    class _Res:
        def fetchall(self):
            return []

    class _Sess:
        async def execute(self, stmt, params=None):
            seen.append(str(stmt))
            return _Res()

    @asynccontextmanager
    async def _bg():
        yield _Sess()

    monkeypatch.setattr(report_cache, "background_session", _bg)
    await ReportQueries._run_all(None, ("SELECT 1", {}), ("SELECT 2", {}))
    timeouts = [s.split("'")[1] for s in seen if "statement_timeout" in s]
    assert timeouts == ["300000", "300000"]
    assert ReportQueries.BACKGROUND_STATEMENT_TIMEOUT_MS == 300_000


def test_report_slots_never_exceed_the_background_pool():
    from app.core.config import get_settings
    st = get_settings()
    pool = st.BACKGROUND_DB_POOL_SIZE + st.BACKGROUND_DB_MAX_OVERFLOW
    assert 1 <= ReportQueries._REPORT_DB_SLOTS._value <= min(2, pool)


def test_loops_and_jobs_never_touch_the_http_pool():
    import inspect
    from app.api.v1.endpoints import reports
    for mod in (report_cache, ReportQueries):
        src = inspect.getsource(mod).replace("BackgroundSessionLocal", "")
        assert "AsyncSessionLocal" not in src, mod.__name__
    assert "AsyncSessionLocal" not in inspect.getsource(reports._run_device_roster_job)


@pytest.mark.asyncio
async def test_background_session_retries_while_the_pool_is_busy(monkeypatch):
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from app.db import base

    attempts, closed = [], []

    class _Sess:
        def __init__(self):
            attempts.append(self)

        async def connection(self):
            if len(attempts) < 3:
                raise PoolTimeout("pool busy")

        async def close(self):
            closed.append(self)

    monkeypatch.setattr(base, "BackgroundSessionLocal", _Sess)
    async with report_cache.background_session() as s:
        assert s is attempts[-1]
    assert len(attempts) == 3 and len(closed) == 3     # 2 failed checkouts + the used one

    # ... but not forever.
    monkeypatch.setattr(report_cache, "BG_CHECKOUT_WAIT_SECS", 0)
    attempts.clear()
    with pytest.raises(PoolTimeout):
        async with report_cache.background_session():
            pass


# ── M6: single-flight take-over, token-checked release, release after cache ───

@pytest.mark.asyncio
async def test_loser_takes_over_when_the_winners_lock_vanishes(fake_redis, monkeypatch):
    """The winner is cancelled (client navigated away): its finally releases the
    lock but nothing was cached. A loser must become the winner at once instead of
    waiting out the whole _LIVE_WAIT_SECS."""
    from app.api.v1.endpoints import reports

    monkeypatch.setattr(reports, "_LIVE_WAIT_SECS", 30.0)
    token = await RedisCache.try_acquire(KEY, ttl=90)          # the doomed winner
    assert token

    async def _winner_cancelled():
        await asyncio.sleep(0.3)
        await RedisCache.release(KEY, token)

    calls = 0

    async def _builder():
        nonlocal calls
        calls += 1
        return [{"n": calls}]

    started = asyncio.get_event_loop().time()
    _, out = await asyncio.gather(
        _winner_cancelled(),
        reports._run_live(_Db(), KEY, _builder, fresh=60, stale=120, single_flight=True),
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert out == [{"n": 1}] and calls == 1
    assert elapsed < 3, f"loser waited {elapsed:.1f}s instead of taking over"
    assert (await RedisCache.cache_get(KEY))[0] == [{"n": 1}]
    assert not [k for k in fake_redis.store if ":lock:" in k]


@pytest.mark.asyncio
async def test_two_losers_after_a_vanished_winner_compute_once(fake_redis, monkeypatch):
    from app.api.v1.endpoints import reports

    monkeypatch.setattr(reports, "_LIVE_WAIT_SECS", 30.0)
    token = await RedisCache.try_acquire(KEY, ttl=90)
    calls = 0

    async def _builder():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.3)
        return [{"n": calls}]

    async def _vanish():
        await asyncio.sleep(0.2)
        await RedisCache.release(KEY, token)

    _, a, b = await asyncio.gather(
        _vanish(),
        reports._run_live(_Db(), KEY, _builder, fresh=60, stale=120, single_flight=True),
        reports._run_live(_Db(), KEY, _builder, fresh=60, stale=120, single_flight=True),
    )
    assert calls == 1 and a == b == [{"n": 1}]


@pytest.mark.asyncio
async def test_release_is_token_checked(fake_redis):
    """A holder whose lock expired must not delete the lock of whoever took over."""
    a = await RedisCache.try_acquire(KEY, ttl=30)
    assert a and isinstance(a, str)
    lock = [k for k in fake_redis.store if ":lock:" in k][0]
    del fake_redis.store[lock]                       # A's TTL elapsed
    b = await RedisCache.try_acquire(KEY, ttl=30)
    assert b and b != a

    await RedisCache.release(KEY, a)                 # stale holder: must be a no-op
    assert lock in fake_redis.store
    assert await RedisCache.try_acquire(KEY, ttl=30) is False      # B still holds it

    await RedisCache.release(KEY, b)                 # the real holder releases
    assert lock not in fake_redis.store
    assert await RedisCache.try_acquire(KEY, ttl=30)


@pytest.mark.asyncio
async def test_fail_open_token_needs_no_release(monkeypatch):
    monkeypatch.setattr(RedisCache, "get_client", lambda: None)
    assert await RedisCache.try_acquire(KEY) is True
    await RedisCache.release(KEY, True)              # no Redis, nothing to do, no error


@pytest.mark.asyncio
async def test_lock_is_released_only_after_the_value_is_cached(fake_redis, monkeypatch):
    from app.api.v1.endpoints import reports

    observed = {}
    real_release = reports.release

    async def _spy(key, token=None):
        observed["cached_at_release"] = (await RedisCache.cache_get(key))[0]
        await real_release(key, token)

    monkeypatch.setattr(reports, "release", _spy)

    async def _builder():
        return [{"v": 1}]

    await reports._run_live(_Db(), KEY, _builder, fresh=60, stale=120, single_flight=True)
    assert observed["cached_at_release"] == [{"v": 1}]


@pytest.mark.asyncio
async def test_fleet_job_caches_before_releasing_the_lock(fake_redis, monkeypatch):
    from app.api.v1.endpoints import reports

    observed = {}
    real_release = reports.release

    async def _spy(key, token=None):
        observed["cached"] = (await RedisCache.cache_get(key))[0]
        await real_release(key, token)

    async def _fleet(*a, **k):
        return {"daily_activity": [], "built": True}

    monkeypatch.setattr(reports, "release", _spy)
    monkeypatch.setattr(reports, "build_fleet_report", _fleet)
    await reports._run_fleet_job("job-1", "c9", "2026-01-01", "2026-01-31", None)
    assert observed["cached"] == {"daily_activity": [], "built": True}


# ── M7: late-arriving data is re-computed ─────────────────────────────────────

@pytest.fixture
def late_env(db_session, monkeypatch):
    """Run the leader's late-data pass against the never-committed test session:
    day recomputes go through db_session, the state read too, no stagger sleeps."""
    from contextlib import asynccontextmanager

    calls = []

    async def _refresh(day, *, advance_to=None):
        calls.append(day)
        await report_cache._compute_day(db_session, day)

    @asynccontextmanager
    async def _bg():
        yield db_session

    monkeypatch.setattr(report_cache, "_refresh_day_committed", _refresh)
    monkeypatch.setattr(report_cache, "background_session", _bg)
    monkeypatch.setattr(report_cache, "LATE_STAGGER_SECS", 0)
    monkeypatch.setattr(report_cache, "_last_trailing_mono", None)
    monkeypatch.setattr(report_cache, "_last_deep_day", None)
    monkeypatch.setattr(report_cache, "_late_pending", [])
    return calls


@pytest.mark.asyncio
async def test_trailing_recompute_picks_up_late_uploaded_events(db_session, late_env):
    today = DAY + timedelta(days=5)          # DAY is 5 days back: inside days 2..7
    deep = today - timedelta(days=12)        # 12 days back: inside the daily 8..30 pass
    company, *_ = await _seed(db_session)
    cid = str(company.id)
    await _set_state(db_session, date(2020, 1, 1))

    # The cache was computed BEFORE the agent uploaded its buffered events.
    for d in (DAY, deep):
        await report_cache._compute_day(db_session, d)
    assert _norm_daily(await report_cache.company_summary_range(db_session, cid, DAY, DAY)) == \
        [(str(DAY), 220, 50, 3)]
    # Late upload: a new device's events for both days, days after the fact.
    late = await _device(db_session, company, "late")
    await _event(db_session, late, app_name="Slack", duration=1000, day=DAY)
    await _event(db_session, late, app_name="Slack", duration=400, day=deep)
    # (deep day had no seed events: its summary is empty until recomputed)
    assert _norm_daily(await report_cache.company_summary_range(db_session, cid, DAY, DAY)) == \
        [(str(DAY), 220, 50, 3)], "stale until the late pass runs"

    more = await report_cache._late_refresh(today)
    assert more is False
    assert _norm_daily(await report_cache.company_summary_range(db_session, cid, DAY, DAY)) == \
        [(str(DAY), 1220, 50, 4)]
    assert _norm_daily(await report_cache.company_summary_range(db_session, cid, deep, deep)) == \
        [(str(deep), 400, 0, 1)]
    assert await report_cache.company_top_apps_range(db_session, cid, DAY, DAY, 5) is not None
    await db_session.rollback()


@pytest.mark.asyncio
async def test_late_pass_schedule_hourly_trailing_and_daily_deep(db_session, late_env, monkeypatch):
    today = date(2020, 6, 30)
    await _set_state(db_session, date(2020, 1, 1))

    await report_cache._late_refresh(today)
    assert sorted(late_env) == sorted(today - timedelta(days=n) for n in range(2, 31))
    assert today not in late_env and today - timedelta(days=1) not in late_env

    # Same hour, same UTC day: nothing is due again.
    late_env.clear()
    await report_cache._late_refresh(today)
    assert late_env == []

    # An hour later: only the trailing 2..7 days re-run.
    monkeypatch.setattr(report_cache, "_last_trailing_mono",
                        report_cache._last_trailing_mono - report_cache.TRAILING_INTERVAL - 1)
    await report_cache._late_refresh(today)
    assert sorted(late_env) == sorted(today - timedelta(days=n) for n in range(2, 8))

    # A new UTC day: deep pass is due again (the trailing one is not).
    late_env.clear()
    await report_cache._late_refresh(today + timedelta(days=1))
    assert sorted(late_env) == sorted(today + timedelta(days=1) - timedelta(days=n) for n in range(8, 31))


@pytest.mark.asyncio
async def test_late_pass_is_budgeted_resumable_and_skips_days_below_the_horizon(db_session, late_env):
    today = date(2020, 6, 30)
    await _set_state(db_session, today - timedelta(days=10))      # horizon 10 days back

    # No time budget: nothing is started, everything stays queued for the next cycle.
    assert await report_cache._late_refresh(today, budget_secs=-1) is True
    assert late_env == [] and report_cache._late_pending

    # Next cycle finishes the queue; days older than the horizon are skipped.
    assert await report_cache._late_refresh(today) is False
    assert sorted(late_env) == sorted(today - timedelta(days=n) for n in range(2, 11))


@pytest.mark.asyncio
async def test_one_failing_late_day_does_not_block_the_rest(db_session, late_env, monkeypatch):
    today = date(2020, 6, 30)
    await _set_state(db_session, date(2020, 1, 1))
    bad = today - timedelta(days=3)
    done = []

    async def _refresh(day, *, advance_to=None):
        if day == bad:
            raise RuntimeError("statement timeout")
        done.append(day)

    monkeypatch.setattr(report_cache, "_refresh_day_committed", _refresh)
    await report_cache._late_refresh(today)
    assert bad not in done and today - timedelta(days=2) in done and today - timedelta(days=30) in done


def test_fast_refresh_no_longer_touches_refreshed_at():
    import inspect
    src = inspect.getsource(report_cache._fast_refresh)
    assert "UPDATE report_daily" not in src and "UPDATE {tbl}" not in src
    assert "refreshed_at" not in src


# ── home.py readers gate on last_fast_refresh, not per-row refreshed_at ───────

async def _seed_home_rows(db, *, today, covered_since, state_age_min):
    """A company with summary + device rows for today and 5 days ago whose OWN
    refreshed_at is hours old (the worker no longer touches them)."""
    company = await _company(db)
    dev = await _device(db, company, "home")
    await _set_state(db, covered_since, age_minutes=state_age_min)
    for d, active in ((today, 300), (today - timedelta(days=5), 120)):
        await db.execute(text("""
            INSERT INTO report_daily_summary
                (company_id, day, active_seconds, idle_seconds, device_count, refreshed_at)
            VALUES (:c, :d, :a, 0, 1, NOW() - INTERVAL '5 hours')
        """), {"c": company.id, "d": d, "a": active})
        await db.execute(text("""
            INSERT INTO report_daily_device_cache
                (company_id, day, device_id, active_seconds, idle_seconds, refreshed_at)
            VALUES (:c, :d, :v, :a, 0, NOW() - INTERVAL '5 hours')
        """), {"c": company.id, "d": d, "v": dev.id, "a": active})
    return company, dev


@pytest.mark.asyncio
async def test_home_readers_trust_a_fresh_fast_cycle_not_row_timestamps(db_session):
    today = datetime.now(timezone.utc).date()
    company, dev = await _seed_home_rows(
        db_session, today=today, covered_since=today - timedelta(days=30), state_age_min=1)

    rows = await report_cache.summary_from_cache(db_session, company.id, 7)
    assert [(r.day, int(r.active_seconds)) for r in rows] == [
        (today, 300), (today - timedelta(days=5), 120)]          # newest first, rows 5 h old
    active = await report_cache.device_active_from_cache(db_session, company.id, today - timedelta(days=7), today)
    assert active == {str(dev.id): 420}
    assert await report_cache.device_active_from_cache(db_session, company.id, today, today) == {str(dev.id): 300}
    await db_session.rollback()


@pytest.mark.asyncio
async def test_home_readers_miss_when_the_fast_cycle_is_stale_or_absent(db_session):
    today = datetime.now(timezone.utc).date()
    company, dev = await _seed_home_rows(
        db_session, today=today, covered_since=today - timedelta(days=30),
        state_age_min=report_cache.STALE_MINUTES_DAILY + 5)

    assert await report_cache.summary_from_cache(db_session, company.id, 7) is None
    assert await report_cache.device_active_from_cache(db_session, company.id, today, today) is None

    await db_session.execute(text("DELETE FROM report_cache_state"))
    assert await report_cache.summary_from_cache(db_session, company.id, 7) is None
    assert await report_cache.device_active_from_cache(db_session, company.id, today, today) is None
    await db_session.rollback()


@pytest.mark.asyncio
async def test_home_readers_only_return_days_inside_the_covered_range(db_session):
    today = datetime.now(timezone.utc).date()
    company, dev = await _seed_home_rows(
        db_session, today=today, covered_since=today - timedelta(days=2), state_age_min=1)

    rows = await report_cache.summary_from_cache(db_session, company.id, 7)
    assert [r.day for r in rows] == [today]                      # the day-5 row is below the horizon
    assert await report_cache.device_active_from_cache(
        db_session, company.id, today - timedelta(days=7), today) == {str(dev.id): 300}

    # A brand-new company (no summary rows at all) is a miss -> the live query decides.
    other = await _company(db_session)
    assert await report_cache.summary_from_cache(db_session, other.id, 7) is None
    assert await report_cache.device_active_from_cache(db_session, other.id, today, today) is None
    await db_session.rollback()


# ── Lifecycle: idempotent start, tolerant stop ────────────────────────────────

@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_tolerates_not_started(monkeypatch):
    started = []

    async def _fake_loop():
        started.append(1)
        await asyncio.sleep(3600)

    monkeypatch.setattr(report_cache, "_loop", _fake_loop)
    monkeypatch.setattr(report_cache, "_worker_task", None)

    await report_cache.stop()                       # never started: fine
    await report_cache.start()
    first = report_cache._worker_task
    await report_cache.start()                      # second start must not orphan a loop
    assert report_cache._worker_task is first
    await asyncio.sleep(0)
    assert started == [1]

    await report_cache.stop()
    assert first.cancelled() and report_cache._worker_task is None
    await report_cache.stop()                       # twice: still fine

    await report_cache.start()                      # and it can be started again
    assert report_cache._worker_task is not first
    await report_cache.stop()
