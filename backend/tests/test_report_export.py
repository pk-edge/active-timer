"""Report export: shared fleet/device_full builders, RBAC scoping, range validation.

Covers:
- build_fleet_report restricts EVERY section (counts, platforms, daily, apps, top
  devices) to the caller's visible devices; an empty scope yields zeros, not the
  whole company.
- The fleet cache key can never be shared between differently-scoped callers.
- build_device_full_report totals equal what the on-screen /device/{id}/summary
  endpoint returns for the same range (one definition of "active").
- POST /v1/reports/jobs and the GET fleet/full endpoints reject bad ranges with a
  400 + human-readable detail (missing, since > until, future until, > 732 days).
- A scoped caller's fleet job (sync path, Redis down) excludes out-of-scope devices.

Same fixture pattern as test_reports_date_range.py / test_device_roster_report.py:
live Postgres, a never-committed session, uuid-tagged rows. The builders are
called with `db=` so they run on that session and see its uncommitted rows.
"""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.reports import MAX_REPORT_RANGE_DAYS, _validate_job_range
from app.db.models import ActivityEvent, Company, Device, Employee, Role, RolePermission
from app.services import ReportQueries
from app.services.ReportQueries import (
    build_device_full_report,
    build_fleet_report,
    fleet_cache_key,
    scope_fingerprint,
)


@pytest.fixture(autouse=True)
def _live_path_only(monkeypatch):
    """These tests seed raw events in a never-committed session and expect the
    live queries. The per-day cache tables can't contain that data, and on a
    database whose report_cache worker has run, `covers()` would route around the
    live path (the cache is exercised in test_report_cache.py)."""
    from app.core import report_cache

    async def _never(db, since, until):
        return False

    monkeypatch.setattr(report_cache, "covers", _never)


# ── Fixture helpers ────────────────────────────────────────────────────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"Exp-{tag}", enrollment_token=f"tok-{tag}")
    db.add(c)
    await db.flush()
    return c, tag


async def _role(db, company, name, *, scope_type="all", is_system=False, perms=()):
    r = Role(company_id=company.id, name=name, scope_type=scope_type, is_system=is_system)
    db.add(r)
    await db.flush()
    for module, action in perms:
        db.add(RolePermission(role_id=r.id, module=module, action=action))
    await db.flush()
    return r


async def _employee(db, company, role, label):
    tag = uuid.uuid4().hex[:6]
    e = Employee(
        company_id=company.id, email=f"{label}-{tag}@test.io", name=f"{label} {tag}",
        role_id=role.id if role else None, is_active=True, is_approved=True,
        password_hash="x",
    )
    db.add(e)
    await db.flush()
    return e


async def _device(db, company, hostname, *, employee=None, platform="macos"):
    d = Device(
        company_id=company.id, hostname=hostname, platform=platform,
        employee_id=employee.id if employee else None,
        is_active=True, last_seen=datetime.now(timezone.utc),
    )
    db.add(d)
    await db.flush()
    return d


async def _event(db, device, *, app_name, duration, timestamp, event_type="window_change"):
    db.add(ActivityEvent(
        device_id=device.id, event_type=event_type, app_name=app_name,
        window_title=app_name, duration=duration, timestamp=timestamp,
    ))
    await db.flush()


def _admin_headers(emp, company):
    from app.core.security import create_admin_token
    return {"Authorization": f"Bearer {create_admin_token(str(emp.id), emp.role, str(company.id))}"}


def _noon(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)


def _range():
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=10), today - timedelta(days=5)


async def _two_device_company(db):
    """Company with in-scope device A and out-of-scope device B, each with
    distinctive activity inside the range."""
    company, _ = await _company(db)
    since, until = _range()
    day = since + timedelta(days=1)
    a = await _device(db, company, "dev-A", platform="macos")
    b = await _device(db, company, "dev-B", platform="win32")
    await _event(db, a, app_name="AppA", duration=100, timestamp=_noon(day))
    await _event(db, a, app_name="AppA", duration=30, timestamp=_noon(day), event_type="idle_start")
    await _event(db, b, app_name="AppB", duration=900, timestamp=_noon(day))
    return company, a, b, since, until


# ── Fleet: scoping ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fleet_scoped_excludes_out_of_scope_devices(db_session):
    company, a, b, since, until = await _two_device_company(db_session)
    cid = str(company.id)

    scoped = await build_fleet_report(cid, since, until, [str(a.id)], db=db_session)

    assert scoped["total_devices"] == 1
    assert [p["platform"] for p in scoped["platform_breakdown"]] == ["Macos"]
    assert sum(d["active_seconds"] for d in scoped["daily_activity"]) == 100
    assert sum(d["idle_seconds"] for d in scoped["daily_activity"]) == 30
    assert all(d["device_count"] == 1 for d in scoped["daily_activity"])
    assert {x["app_name"] for x in scoped["top_apps"]} == {"AppA"}
    assert [x["device_id"] for x in scoped["most_active_devices"]] == [str(a.id)]

    # None = company-wide: the same data now includes the other device.
    everything = await build_fleet_report(cid, since, until, None, db=db_session)
    assert everything["total_devices"] == 2
    assert sum(d["active_seconds"] for d in everything["daily_activity"]) == 1000
    assert {x["app_name"] for x in everything["top_apps"]} == {"AppA", "AppB"}
    assert {x["device_id"] for x in everything["most_active_devices"]} == {str(a.id), str(b.id)}

    await db_session.rollback()


@pytest.mark.asyncio
async def test_fleet_empty_scope_yields_zeros_not_everything(db_session):
    company, _a, _b, since, until = await _two_device_company(db_session)

    empty = await build_fleet_report(str(company.id), since, until, [], db=db_session)

    assert empty["total_devices"] == 0
    assert empty["online_devices"] == 0
    assert empty["offline_devices"] == 0
    assert empty["archived_devices"] == 0
    assert empty["platform_breakdown"] == []
    assert empty["daily_activity"] == []
    assert empty["top_apps"] == []
    assert empty["most_active_devices"] == []

    await db_session.rollback()


@pytest.mark.asyncio
async def test_fleet_range_is_inclusive_half_open(db_session):
    company, _ = await _company(db_session)
    since, until = _range()
    dev = await _device(db_session, company, "edge")
    start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    end_next = datetime.combine(until + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    await _event(db_session, dev, app_name="First", duration=1, timestamp=start)                      # in
    await _event(db_session, dev, app_name="Last", duration=2, timestamp=end_next - timedelta(seconds=1))  # in
    await _event(db_session, dev, app_name="Before", duration=4, timestamp=start - timedelta(seconds=1))   # out
    await _event(db_session, dev, app_name="After", duration=8, timestamp=end_next)                   # out

    result = await build_fleet_report(str(company.id), since, until, None, db=db_session)
    assert {x["app_name"] for x in result["top_apps"]} == {"First", "Last"}

    await db_session.rollback()


def test_fleet_cache_key_differs_by_scope():
    cid, since, until = str(uuid.uuid4()), date(2026, 1, 1), date(2026, 1, 31)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())

    keys = {
        "all":    fleet_cache_key(cid, since, until, None),
        "empty":  fleet_cache_key(cid, since, until, []),
        "a":      fleet_cache_key(cid, since, until, [a]),
        "ab":     fleet_cache_key(cid, since, until, [a, b]),
    }
    assert len(set(keys.values())) == 4, "scoped / empty / unscoped must never share an entry"

    # Same visible set in a different order is the same scope.
    assert fleet_cache_key(cid, since, until, [a, b]) == fleet_cache_key(cid, since, until, [b, a])
    # Range and company still partition the key.
    assert fleet_cache_key(cid, since, until, None) != fleet_cache_key(cid, since, date(2026, 2, 1), None)
    assert fleet_cache_key(cid, since, until, None) != fleet_cache_key(str(uuid.uuid4()), since, until, None)
    assert scope_fingerprint(None) == "all"


# ── device_full ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_full_totals_match_on_screen_summary(client, db_session):
    company, tag = await _company(db_session)
    role = await _role(db_session, company, f"Owner-{tag}", scope_type="all", is_system=True)
    admin = await _employee(db_session, company, role, "owner")
    owner = await _employee(db_session, company, None, "talent")
    dev = await _device(db_session, company, "full-host", employee=owner)

    since, until = _range()
    for i in range(4):
        day = since + timedelta(days=i)
        await _event(db_session, dev, app_name="Code", duration=100 + i, timestamp=_noon(day))
        await _event(db_session, dev, app_name="Idle", duration=999, timestamp=_noon(day))  # excluded
        await _event(db_session, dev, app_name=None, duration=10 + i, timestamp=_noon(day),
                     event_type="idle_start")
    # outside the range — must not leak in
    await _event(db_session, dev, app_name="Code", duration=5000,
                 timestamp=_noon(until + timedelta(days=1)))

    report = await build_device_full_report(
        str(company.id), str(dev.id), since, until, db=db_session,
    )
    assert report is not None
    assert report["device"]["employee_name"] == owner.name

    headers = _admin_headers(admin, company)
    params = {"since": since.isoformat(), "until": until.isoformat()}
    r = await client.get(f"/v1/reports/device/{dev.id}/summary", params=params, headers=headers)
    assert r.status_code == 200
    summary = r.json()
    assert report["total_active_seconds"] == sum(d["active_seconds"] for d in summary) == 406
    assert report["total_idle_seconds"] == sum(d["idle_seconds"] for d in summary) == 46
    assert report["working_days"] == 4
    assert len(report["top_apps"]) == 1, "idle apps must be excluded"

    # The on-screen /full endpoint is the same builder, so it agrees too.
    r = await client.get(f"/v1/reports/device/{dev.id}/full", params=params, headers=headers)
    assert r.status_code == 200
    assert r.json()["total_active_seconds"] == report["total_active_seconds"]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_device_full_unknown_device_returns_none(db_session):
    company, _ = await _company(db_session)
    since, until = _range()
    assert await build_device_full_report(
        str(company.id), str(uuid.uuid4()), since, until, db=db_session,
    ) is None
    await db_session.rollback()


# ── Range validation ───────────────────────────────────────────────────────────

def _today():
    return datetime.now(timezone.utc).date()


def _detail(since, until):
    with pytest.raises(HTTPException) as exc:
        _validate_job_range(since, until)
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, str) and exc.value.detail
    return exc.value.detail


def test_validate_range_missing_dates():
    today = _today().isoformat()
    for since, until in ((None, None), (today, None), (None, today), ("", today), (today, "")):
        _detail(since, until)


def test_validate_range_invalid_format():
    assert "since" in _detail("not-a-date", _today().isoformat())


def test_validate_range_since_after_until():
    t = _today()
    assert "since" in _detail(t.isoformat(), (t - timedelta(days=1)).isoformat())


def test_validate_range_future_until():
    t = _today()
    assert "future" in _detail(t.isoformat(), (t + timedelta(days=1)).isoformat())


def test_validate_range_span_limit():
    t = _today()
    assert MAX_REPORT_RANGE_DAYS == 732

    # Exactly 732 inclusive days is fine, and so is a single day / today.
    ok_since = t - timedelta(days=MAX_REPORT_RANGE_DAYS - 1)
    assert _validate_job_range(ok_since.isoformat(), t.isoformat()) == (ok_since, t)
    assert _validate_job_range(t.isoformat(), t.isoformat()) == (t, t)

    # 733 inclusive days is not.
    too_long = t - timedelta(days=MAX_REPORT_RANGE_DAYS)
    assert str(MAX_REPORT_RANGE_DAYS) in _detail(too_long.isoformat(), t.isoformat())


def test_validate_range_last_two_years_with_leap_day():
    # "Last 2 Years" = subYears(today, 2)..today. Spanning Feb 29 makes the
    # inclusive span 732 days, which must still be accepted.
    since, until = date(2023, 6, 1), date(2025, 6, 1)  # spans Feb 29 2024
    assert (until - since).days + 1 == 732
    assert _validate_job_range(since.isoformat(), until.isoformat()) == (since, until)


@pytest.mark.asyncio
async def test_run_all_failure_cancels_siblings_and_creates_no_stray_coroutines(monkeypatch):
    started, cancelled = [], []

    async def fake_fetch(db, sql, params, timeout_ms=None):
        started.append(sql)
        if sql == "boom":
            raise RuntimeError("boom")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(sql)
            raise

    monkeypatch.setattr(ReportQueries, "_fetch", fake_fetch)
    with pytest.raises(RuntimeError):
        await ReportQueries._run_all(None, ("slow1", {}), ("boom", {}), ("slow2", {}))
    assert sorted(cancelled) == ["slow1", "slow2"]

    # Sequential path: queries after the failing one are never even started.
    started.clear()

    class _Db:
        async def execute(self, *_a, **_k):
            return None

    with pytest.raises(RuntimeError):
        await ReportQueries._run_all(_Db(), ("boom", {}), ("never", {}))
    assert started == ["boom"]


@pytest.mark.asyncio
async def test_jobs_and_get_endpoints_return_400_for_bad_ranges(client, db_session):
    company, tag = await _company(db_session)
    role = await _role(db_session, company, f"Owner-{tag}", scope_type="all", is_system=True)
    admin = await _employee(db_session, company, role, "owner")
    dev = await _device(db_session, company, "val-host")
    headers = _admin_headers(admin, company)

    t = _today()
    bad = [
        {},                                                                          # missing both
        {"since": t.isoformat()},                                                    # missing until
        {"since": t.isoformat(), "until": (t - timedelta(days=1)).isoformat()},      # since > until
        {"since": t.isoformat(), "until": (t + timedelta(days=1)).isoformat()},      # future
        {"since": (t - timedelta(days=MAX_REPORT_RANGE_DAYS)).isoformat(),
         "until": t.isoformat()},                                                    # 733 days
    ]
    for rng in bad:
        for body in ({"type": "fleet", **rng}, {"type": "device_full", "device_id": str(dev.id), **rng}):
            r = await client.post("/v1/reports/jobs", json=body, headers=headers)
            assert r.status_code == 400, (body, r.text)
            assert isinstance(r.json()["detail"], str)

        if "since" in rng and "until" in rng:
            for path in ("/v1/reports/devices/fleet", f"/v1/reports/device/{dev.id}/full"):
                r = await client.get(path, params=rng, headers=headers)
                assert r.status_code == 400, (path, rng, r.text)

    # device_roster takes no range at all, so bad/missing dates never block it.
    r = await client.post("/v1/reports/jobs", json={"type": "device_roster"}, headers=headers)
    assert r.status_code == 202

    await db_session.rollback()


# ── End to end: a scoped caller's fleet job (sync fallback, Redis down) ────────

@pytest.mark.asyncio
async def test_scoped_caller_fleet_job_excludes_out_of_scope_devices(client, db_session):
    company, tag = await _company(db_session)
    role = await _role(db_session, company, f"Own-{tag}", scope_type="own",
                       perms=[("reports", "view")])
    me = await _employee(db_session, company, role, "scoped")
    other = await _employee(db_session, company, None, "other")
    mine = await _device(db_session, company, "mine", employee=me)
    theirs = await _device(db_session, company, "theirs", employee=other)

    since, until = _range()
    day = since + timedelta(days=2)
    await _event(db_session, mine, app_name="Mine", duration=60, timestamp=_noon(day))
    await _event(db_session, theirs, app_name="Theirs", duration=600, timestamp=_noon(day))

    headers = _admin_headers(me, company)
    body = {"type": "fleet", "since": since.isoformat(), "until": until.isoformat()}
    r = await client.post("/v1/reports/jobs", json=body, headers=headers)
    assert r.status_code == 202
    job = r.json()
    if job["status"] != "done":
        pytest.skip("Redis is up: job runs in a background worker on separate sessions")

    result = job["result"]
    assert result["total_devices"] == 1
    assert {x["app_name"] for x in result["top_apps"]} == {"Mine"}
    assert [x["hostname"] for x in result["most_active_devices"]] == ["mine"]

    # The on-screen GET endpoint is scoped identically.
    r = await client.get("/v1/reports/devices/fleet", params={
        "since": since.isoformat(), "until": until.isoformat()}, headers=headers)
    assert r.status_code == 200
    assert r.json()["total_devices"] == 1
    assert [x["hostname"] for x in r.json()["most_active_devices"]] == ["mine"]

    await db_session.rollback()


# ── One window / one active-time definition for screen, cache and export ──────

def test_days_window_is_last_n_calendar_days_ending_today():
    from app.api.v1.endpoints.reports import _resolve_window
    today = datetime.now(timezone.utc).date()
    assert _resolve_window(7, None, None) == (today - timedelta(days=6), today)
    assert _resolve_window(1, None, None) == (today, today)
    fixed = (date(2026, 1, 1), date(2026, 1, 31))
    assert _resolve_window(7, *fixed) == fixed


def test_days_and_equivalent_since_until_share_a_cache_key():
    from app.api.v1.endpoints.reports import _range_cache_fp
    today = datetime.now(timezone.utc).date()
    assert _range_cache_fp(7, None, None) == _range_cache_fp(7, today - timedelta(days=6), today)
    assert _range_cache_fp(7, None, None) != _range_cache_fp(30, None, None)


@pytest.mark.asyncio
async def test_macos_lock_screen_is_not_active_time(client, db_session):
    """loginwindow / ScreenSaverEngine are idle on the home dashboard and in the
    report cache; reports and exports must agree."""
    company, _ = await _company(db_session)
    since, until = _range()
    day = since + timedelta(days=1)
    dev = await _device(db_session, company, "mac-lock")
    await _event(db_session, dev, app_name="Xcode", duration=200, timestamp=_noon(day))
    await _event(db_session, dev, app_name="loginwindow", duration=5000, timestamp=_noon(day))
    await _event(db_session, dev, app_name="ScreenSaverEngine", duration=7000, timestamp=_noon(day))

    full = await build_device_full_report(str(company.id), str(dev.id), since, until, db=db_session)
    assert full["total_active_seconds"] == 200
    assert [a["app_name"] for a in full["top_apps"]] == ["Xcode"]

    fleet = await build_fleet_report(str(company.id), since, until, None, db=db_session)
    assert sum(d["active_seconds"] for d in fleet["daily_activity"]) == 200
    assert [a["app_name"] for a in fleet["top_apps"]] == ["Xcode"]
