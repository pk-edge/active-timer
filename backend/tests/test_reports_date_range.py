"""GET /v1/reports/company/summary, /company/top-apps, /device/{id}/summary,
/device/{id}/top-apps — since/until real calendar-range support (added
alongside the existing `days`-rolling-window model).

Covers:
- `days`-only requests are unchanged (regression guard for the default path).
- since/until produce the correct INCLUSIVE [since, until] range, verified
  with events seeded just inside and just outside both boundaries, for both
  the company-wide and per-device summary/top-apps endpoints.
- The Redis response-cache key now incorporates since/until, so a since/until
  query (where `until` != today) can never be served a `days`-keyed cache
  entry, and vice versa — verified against a REAL Redis (started/stopped for
  just that one test) rather than mocked, since the whole point is the actual
  key construction.
- Validation: since without until (and vice versa) is 400; since > until is
  400; since/until alongside a non-default `days` is 400.
- The `reports:view` RBAC gate is unchanged.

Same fixture pattern as test_logs_pagination.py / test_search.py: live
Postgres, a never-committed session (rolled back by the `db_session`/`client`
fixtures in conftest.py), uuid-tagged fixtures so parallel runs never collide.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import ActivityEvent, Company, Device, Employee, Role, RolePermission


# ── Fixture helpers (mirrors test_logs_pagination.py) ──────────────────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"Rpt-{tag}", enrollment_token=f"tok-{tag}")
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


async def _device(db, company, hostname):
    d = Device(
        company_id=company.id, hostname=hostname, platform="macos",
        is_active=True, last_seen=datetime.now(timezone.utc),
    )
    db.add(d)
    await db.flush()
    return d


async def _event(db, device, *, app_name, duration, timestamp, event_type="window_change"):
    ev = ActivityEvent(
        device_id=device.id, event_type=event_type, app_name=app_name,
        window_title=app_name, duration=duration, timestamp=timestamp,
    )
    db.add(ev)
    await db.flush()
    return ev


def _admin_headers(emp, company):
    from app.core.security import create_admin_token
    return {"Authorization": f"Bearer {create_admin_token(str(emp.id), emp.role, str(company.id))}"}


async def _all_scope_admin(db):
    company, tag = await _company(db)
    role = await _role(db, company, "Owner", scope_type="all", is_system=True)
    admin = await _employee(db, company, role, "owner")
    return company, admin


# ── days-only regression guard ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_days_only_company_summary_and_top_apps_unchanged(client, db_session):
    """No since/until at all — must behave exactly as the pre-existing
    days-rolling-window model (the default path this feature must not regress)."""
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "days-host")

    now = datetime.now(timezone.utc)
    await _event(db_session, device, app_name="Recent1", duration=100, timestamp=now - timedelta(hours=2))
    await _event(db_session, device, app_name="Recent2", duration=50, timestamp=now - timedelta(hours=3))
    # Outside the 7-day window entirely — must not be counted.
    await _event(db_session, device, app_name="TooOld", duration=999, timestamp=now - timedelta(days=10))

    headers = _admin_headers(admin, company)

    r = await client.get("/v1/reports/company/summary", params={"days": 7}, headers=headers)
    assert r.status_code == 200
    total_active = sum(d["active_seconds"] for d in r.json())
    assert total_active == 150

    r = await client.get(
        "/v1/reports/company/top-apps", params={"days": 7, "limit": 50}, headers=headers,
    )
    assert r.status_code == 200
    apps = {a["app_name"]: a["seconds"] for a in r.json()}
    assert apps.get("Recent1") == 100
    assert apps.get("Recent2") == 50
    assert "TooOld" not in apps

    r = await client.get(
        f"/v1/reports/device/{device.id}/summary", params={"days": 7}, headers=headers,
    )
    assert r.status_code == 200
    assert sum(d["active_seconds"] for d in r.json()) == 150

    r = await client.get(
        f"/v1/reports/device/{device.id}/top-apps", params={"days": 7}, headers=headers,
    )
    assert r.status_code == 200
    dev_apps = {a["app_name"]: a["seconds"] for a in r.json()}
    assert dev_apps.get("Recent1") == 100
    assert dev_apps.get("Recent2") == 50
    assert "TooOld" not in dev_apps

    await db_session.rollback()


# ── since/until inclusive-range correctness ────────────────────────────────────

async def _seed_boundary_events(db, device, since_dt, until_dt):
    """Four events straddling both ends of [since_dt, until_dt]: one clearly
    inside each boundary day, one clearly outside each boundary day."""
    await _event(db, device, app_name="InsideSince", duration=111,
                 timestamp=datetime.combine(since_dt, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1))
    await _event(db, device, app_name="OutsideBeforeSince", duration=222,
                 timestamp=datetime.combine(since_dt, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=1))
    await _event(db, device, app_name="InsideUntil", duration=333,
                 timestamp=datetime.combine(until_dt, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23))
    await _event(db, device, app_name="OutsideAfterUntil", duration=444,
                 timestamp=datetime.combine(until_dt, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1, hours=1))


@pytest.mark.asyncio
async def test_since_until_inclusive_range_company_summary_and_top_apps(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "range-host")

    today = datetime.now(timezone.utc).date()
    since_dt = today - timedelta(days=20)
    until_dt = today - timedelta(days=10)
    await _seed_boundary_events(db_session, device, since_dt, until_dt)

    headers = _admin_headers(admin, company)
    params = {"since": since_dt.isoformat(), "until": until_dt.isoformat()}

    r = await client.get("/v1/reports/company/summary", params=params, headers=headers)
    assert r.status_code == 200
    total_active = sum(d["active_seconds"] for d in r.json())
    assert total_active == 111 + 333, "must include both in-range events and exclude both out-of-range ones"

    r = await client.get(
        "/v1/reports/company/top-apps", params={**params, "limit": 50}, headers=headers,
    )
    assert r.status_code == 200
    apps = {a["app_name"] for a in r.json()}
    assert apps == {"InsideSince", "InsideUntil"}

    await db_session.rollback()


@pytest.mark.asyncio
async def test_since_until_inclusive_range_device_summary_and_top_apps(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "range-dev-host")

    today = datetime.now(timezone.utc).date()
    since_dt = today - timedelta(days=40)
    until_dt = today - timedelta(days=30)
    await _seed_boundary_events(db_session, device, since_dt, until_dt)

    headers = _admin_headers(admin, company)
    params = {"since": since_dt.isoformat(), "until": until_dt.isoformat()}

    r = await client.get(f"/v1/reports/device/{device.id}/summary", params=params, headers=headers)
    assert r.status_code == 200
    total_active = sum(d["active_seconds"] for d in r.json())
    assert total_active == 111 + 333

    r = await client.get(f"/v1/reports/device/{device.id}/top-apps", params=params, headers=headers)
    assert r.status_code == 200
    apps = {a["app_name"] for a in r.json()}
    assert apps == {"InsideSince", "InsideUntil"}

    await db_session.rollback()


# ── Cache-key correctness: since/until must never leak a `days`-keyed hit ─────

@pytest.mark.asyncio
async def test_since_until_and_days_never_share_a_cache_entry(client, db_session):
    """The Redis response cache key MUST incorporate since/until — otherwise a
    since/until query (until != today) could be served a stale `days`-keyed
    result, or vice versa. Exercised against a REAL Redis (started for just
    this test) since the whole point under test is the actual key
    construction, not a mock that can't catch a wrong key.
    """
    from app.core import RedisClient

    await RedisClient.startup()
    if RedisClient.get_client() is None:
        pytest.skip("Redis not reachable in this test environment")

    try:
        company, admin = await _all_scope_admin(db_session)
        device = await _device(db_session, company, "cache-host")
        headers = _admin_headers(admin, company)

        now = datetime.now(timezone.utc)
        # "days" shape: recent event, inside the default days=7 rolling window.
        await _event(db_session, device, app_name="RollingWindowApp", duration=700,
                     timestamp=now - timedelta(hours=1))

        # "since/until" shape: a fixed range far in the past, until != today,
        # with a distinct total so any leak between the two shapes is obvious.
        today = datetime.now(timezone.utc).date()
        since_dt = today - timedelta(days=60)
        until_dt = today - timedelta(days=50)
        await _event(db_session, device, app_name="FixedRangeApp", duration=300,
                     timestamp=datetime.combine(since_dt, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1))
        range_params = {"since": since_dt.isoformat(), "until": until_dt.isoformat()}

        # 1) Warm the plain `days=7` cache entry first.
        r1 = await client.get("/v1/reports/company/summary", params={"days": 7}, headers=headers)
        assert r1.status_code == 200
        assert sum(d["active_seconds"] for d in r1.json()) == 700

        # 2) Immediately query the since/until range — must compute the ACTUAL
        #    fixed-range total (300), not the days-keyed cached 700.
        r2 = await client.get("/v1/reports/company/summary", params=range_params, headers=headers)
        assert r2.status_code == 200
        assert sum(d["active_seconds"] for d in r2.json()) == 300, \
            "since/until query must not be served the days-keyed cache entry"

        # 3) Query the since/until range again — now warmed in its OWN cache
        #    entry; must still return the correct 300 (its own cache is correct,
        #    not merely "not the other one's").
        r3 = await client.get("/v1/reports/company/summary", params=range_params, headers=headers)
        assert r3.status_code == 200
        assert sum(d["active_seconds"] for d in r3.json()) == 300

        # 4) And the reverse direction: the days=7 query must still return its
        #    own correct total, not anything leaked from the since/until entry.
        r4 = await client.get("/v1/reports/company/summary", params={"days": 7}, headers=headers)
        assert r4.status_code == 200
        assert sum(d["active_seconds"] for d in r4.json()) == 700

        await db_session.rollback()
    finally:
        await RedisClient.shutdown()


# ── Validation ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_since_without_until_is_400(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    headers = _admin_headers(admin, company)
    r = await client.get(
        "/v1/reports/company/summary", params={"since": "2026-01-01"}, headers=headers,
    )
    assert r.status_code == 400
    await db_session.rollback()


@pytest.mark.asyncio
async def test_until_without_since_is_400(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    headers = _admin_headers(admin, company)
    r = await client.get(
        "/v1/reports/company/summary", params={"until": "2026-01-01"}, headers=headers,
    )
    assert r.status_code == 400
    await db_session.rollback()


@pytest.mark.asyncio
async def test_since_after_until_is_400(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    headers = _admin_headers(admin, company)
    r = await client.get(
        "/v1/reports/company/summary",
        params={"since": "2026-02-01", "until": "2026-01-01"},
        headers=headers,
    )
    assert r.status_code == 400
    await db_session.rollback()


@pytest.mark.asyncio
async def test_since_until_with_nondefault_days_is_400(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    headers = _admin_headers(admin, company)
    r = await client.get(
        "/v1/reports/company/summary",
        params={"since": "2026-01-01", "until": "2026-01-31", "days": 14},
        headers=headers,
    )
    assert r.status_code == 400
    await db_session.rollback()


@pytest.mark.asyncio
async def test_since_until_applies_to_all_four_endpoints_validation(client, db_session):
    """Same 400 rule holds on top-apps and the per-device variants, not just
    company/summary."""
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "validate-host")
    headers = _admin_headers(admin, company)

    for path in (
        "/v1/reports/company/top-apps",
        f"/v1/reports/device/{device.id}/summary",
        f"/v1/reports/device/{device.id}/top-apps",
    ):
        r = await client.get(path, params={"since": "2026-01-01"}, headers=headers)
        assert r.status_code == 400, path
        r = await client.get(
            path, params={"since": "2026-02-01", "until": "2026-01-01"}, headers=headers,
        )
        assert r.status_code == 400, path

    await db_session.rollback()


# ── RBAC gate unchanged ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reports_view_gate_still_required(client, db_session):
    company, tag = await _company(db_session)
    no_perms_role = await _role(db_session, company, f"NoPerms-{tag}", scope_type="own", perms=())
    plain = await _employee(db_session, company, no_perms_role, "plain")
    headers = _admin_headers(plain, company)

    r = await client.get("/v1/reports/company/summary", params={"days": 7}, headers=headers)
    assert r.status_code == 403

    r = await client.get(
        "/v1/reports/company/summary",
        params={"since": "2026-01-01", "until": "2026-01-31"},
        headers=headers,
    )
    assert r.status_code == 403, "gate must apply before/regardless of since-until validation"

    await db_session.rollback()
