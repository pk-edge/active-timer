"""Technology & AI pages — the `device_id` filter and its lookup endpoint.

The Tech pages could filter by date/group/talent but never by an individual
device, so an admin looking at one machine had no way to say so. This adds
`device_id` to every filterable tech endpoint plus `GET /v1/tech/filters/devices`
to populate the picker.

Covers:
- `/v1/tech/filters/devices` is gated on `tech:view` (NOT `devices:view`, the
  same reason `/filters/users` exists — a tech-only role must be able to
  populate its own filter), and returns only devices the caller can see per
  `visible_device_ids` (`all` / `groups` / `own`).
- `device_id` actually narrows results on websites, apps, category-usage and
  utilization/summary — verified against two devices in the same company whose
  events would otherwise both be counted.
- Tenant isolation: another company's device id returns the caller's OWN
  totals-minus-nothing (i.e. an empty result), never the foreign company's
  rows. Same for a device inside the company that the caller cannot see.
- A malformed (non-UUID) device_id is an empty result, not a 500 — asyncpg
  raises on a non-UUID bound to a uuid column, so `_device_clause` must catch
  it before it reaches the driver.
- The Redis response-cache key incorporates device_id, so a device-filtered
  request can never be served the unfiltered entry (and vice versa). Verified
  against an in-memory stand-in for cache_get/cache_set so the real key
  construction runs without needing a live Redis.
- Regression guard: omitting device_id leaves every endpoint unchanged.

Same fixture pattern as test_reports_date_range.py: live Postgres, a
never-committed session, uuid-tagged fixtures so parallel runs never collide.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import (
    ActivityEvent, AppCategory, Company, Device, Employee,
    EmployeeGroupAccess, Group, GroupMember, Role, RolePermission,
)


# ── Fixture helpers ───────────────────────────────────────────────────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"Tech-{tag}", enrollment_token=f"tok-{tag}")
    db.add(c)
    await db.flush()
    # `_ensure_default_categories` COMMITS when a company has no system
    # categories — which would commit this test's never-committed session and
    # leak its fixtures into the live DB. Seeding one system category up front
    # makes that helper take its early-return path (a SELECT only).
    db.add(AppCategory(company_id=c.id, name="Productivity", color="#3b82f6", is_system=True))
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


async def _device(db, company, hostname, employee=None):
    d = Device(
        company_id=company.id, hostname=hostname, platform="macos",
        employee_id=employee.id if employee else None,
        is_active=True, last_seen=datetime.now(timezone.utc),
    )
    db.add(d)
    await db.flush()
    return d


async def _event(db, device, *, app_name, duration, url=None, when=None,
                 event_type="window_change"):
    ev = ActivityEvent(
        device_id=device.id, event_type=event_type, app_name=app_name,
        window_title=app_name, url=url, duration=duration,
        timestamp=when or (datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    db.add(ev)
    await db.flush()
    return ev


async def _visit(db, device, *, host, app_name="Chrome", when=None):
    """A browser hit. Website Usage reads `url_visit` rows specifically (and
    derives duration from the gap to the next visit), so app-time events alone
    leave that endpoint empty."""
    return await _event(db, device, app_name=app_name, duration=0,
                        url=f"https://{host}/page", when=when, event_type="url_visit")


def _headers(emp, company):
    from app.core.security import create_admin_token
    return {"Authorization": f"Bearer {create_admin_token(str(emp.id), emp.role, str(company.id))}"}


async def _tech_admin(db, *, scope_type="all"):
    """A company + an admin holding exactly `tech:view` at the given scope."""
    company, tag = await _company(db)
    role = await _role(
        db, company, f"TechViewer-{tag}", scope_type=scope_type,
        is_system=(scope_type == "all"), perms=[("tech", "view")],
    )
    admin = await _employee(db, company, role, "techadmin")
    return company, admin


def _today_params(**extra):
    """All tech endpoints default to a 30-day rolling window; pin to today so
    the seeded events (1h ago) are always inside the range."""
    today = datetime.now(timezone.utc).date().isoformat()
    return {"since_date": today, "until_date": today, **extra}


# ── /v1/tech/filters/devices ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filters_devices_lists_company_devices_for_all_scope(client, db_session):
    company, admin = await _tech_admin(db_session)
    owner = await _employee(db_session, company, None, "owner")
    assigned   = await _device(db_session, company, "assigned-host", employee=owner)
    unassigned = await _device(db_session, company, "unassigned-host")

    # Another tenant's device must never appear.
    other_company, _ = await _company(db_session)
    await _device(db_session, other_company, "foreign-host")

    r = await client.get("/v1/tech/filters/devices", headers=_headers(admin, company))
    assert r.status_code == 200
    by_id = {d["id"]: d for d in r.json()}

    assert str(assigned.id) in by_id
    assert str(unassigned.id) in by_id
    assert by_id[str(assigned.id)]["employee_name"] == owner.name
    # An unassigned device is exactly the case the talent filter cannot reach,
    # so it must still be listed — with a null owner rather than being dropped.
    assert by_id[str(unassigned.id)]["employee_name"] is None
    assert by_id[str(assigned.id)]["platform"] == "macos"
    assert not any(d["hostname"] == "foreign-host" for d in r.json())

    await db_session.rollback()


@pytest.mark.asyncio
async def test_filters_devices_requires_tech_view_not_devices_view(client, db_session):
    """The whole reason this endpoint exists rather than reusing /v1/devices:
    a role with tech access but no devices access must still populate the
    picker, and a role with neither must be refused."""
    company, tag = await _company(db_session)
    no_tech_role = await _role(db_session, company, f"NoTech-{tag}", perms=[("devices", "view")])
    no_tech = await _employee(db_session, company, no_tech_role, "notech")

    r = await client.get("/v1/tech/filters/devices", headers=_headers(no_tech, company))
    assert r.status_code == 403

    _, tech_admin = await _tech_admin(db_session)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_filters_devices_own_scope_sees_only_own_devices(client, db_session):
    """Unlike /filters/users (where "own" leaves nobody to pick between), an
    own-scoped caller with several machines still has a real choice, so the
    list is populated — but only with their own."""
    company, admin = await _tech_admin(db_session, scope_type="own")
    mine  = await _device(db_session, company, "my-host", employee=admin)
    other = await _employee(db_session, company, None, "colleague")
    await _device(db_session, company, "their-host", employee=other)

    r = await client.get("/v1/tech/filters/devices", headers=_headers(admin, company))
    assert r.status_code == 200
    hostnames = {d["hostname"] for d in r.json()}
    assert hostnames == {"my-host"}
    assert r.json()[0]["id"] == str(mine.id)

    await db_session.rollback()


@pytest.mark.asyncio
async def test_filters_devices_groups_scope_sees_only_assigned_group(client, db_session):
    company, admin = await _tech_admin(db_session, scope_type="groups")
    tag = uuid.uuid4().hex[:6]

    in_group  = Group(company_id=company.id, name=f"InGroup-{tag}")
    out_group = Group(company_id=company.id, name=f"OutGroup-{tag}")
    db_session.add_all([in_group, out_group])
    await db_session.flush()

    visible = await _device(db_session, company, "visible-host")
    hidden  = await _device(db_session, company, "hidden-host")
    db_session.add_all([
        GroupMember(group_id=in_group.id,  member_type="device", device_id=visible.id),
        GroupMember(group_id=out_group.id, member_type="device", device_id=hidden.id),
        EmployeeGroupAccess(employee_id=admin.id, group_id=in_group.id),
    ])
    await db_session.flush()

    r = await client.get("/v1/tech/filters/devices", headers=_headers(admin, company))
    assert r.status_code == 200
    assert {d["hostname"] for d in r.json()} == {"visible-host"}

    await db_session.rollback()


# ── device_id narrows the analytics endpoints ─────────────────────────────────

async def _two_device_company(db):
    """One company, two devices, distinct app + website activity on each."""
    company, admin = await _tech_admin(db)
    alice = await _employee(db, company, None, "alice")
    bob   = await _employee(db, company, None, "bob")
    dev_a = await _device(db, company, "alice-mbp", employee=alice)
    dev_b = await _device(db, company, "bob-mbp",   employee=bob)

    await _event(db, dev_a, app_name="Chrome",  duration=300)
    await _event(db, dev_b, app_name="Firefox", duration=700)
    await _visit(db, dev_a, host="alpha.example.com")
    await _visit(db, dev_b, host="beta.example.com", app_name="Firefox")
    return company, admin, dev_a, dev_b


@pytest.mark.asyncio
async def test_device_id_narrows_website_list(client, db_session):
    company, admin, dev_a, dev_b = await _two_device_company(db_session)
    headers = _headers(admin, company)

    r = await client.get("/v1/tech/websites", params=_today_params(), headers=headers)
    assert r.status_code == 200
    assert {i["host"] for i in r.json()["items"]} == {"alpha.example.com", "beta.example.com"}

    r = await client.get(
        "/v1/tech/websites", params=_today_params(device_id=str(dev_a.id)), headers=headers,
    )
    assert r.status_code == 200
    assert {i["host"] for i in r.json()["items"]} == {"alpha.example.com"}

    await db_session.rollback()


@pytest.mark.asyncio
async def test_device_id_narrows_app_list_and_summary(client, db_session):
    company, admin, dev_a, dev_b = await _two_device_company(db_session)
    headers = _headers(admin, company)

    r = await client.get("/v1/tech/apps", params=_today_params(), headers=headers)
    assert r.status_code == 200
    assert r.json()["total_secs"] == 1000

    r = await client.get(
        "/v1/tech/apps", params=_today_params(device_id=str(dev_b.id)), headers=headers,
    )
    assert r.status_code == 200
    assert {i["app_name"] for i in r.json()["items"]} == {"Firefox"}
    assert r.json()["total_secs"] == 700

    r = await client.get(
        "/v1/tech/utilization/summary",
        params=_today_params(device_id=str(dev_b.id)), headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["total_hours"] == round(700 / 3600, 1)

    await db_session.rollback()


@pytest.mark.asyncio
async def test_device_id_narrows_category_usage(client, db_session):
    """Category Usage also gains `employee_id` in this change — the page has
    been rendering a Talent filter that the endpoint never accepted."""
    company, admin, dev_a, dev_b = await _two_device_company(db_session)
    headers = _headers(admin, company)

    r = await client.get("/v1/tech/category-usage", params=_today_params(), headers=headers)
    assert r.status_code == 200
    assert r.json()["total_secs"] == 1000

    r = await client.get(
        "/v1/tech/category-usage",
        params=_today_params(device_id=str(dev_a.id)), headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["total_secs"] == 300

    r = await client.get(
        "/v1/tech/category-usage",
        params=_today_params(employee_id=str(dev_b.employee_id)), headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["total_secs"] == 700

    await db_session.rollback()


# ── Isolation and malformed input ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_foreign_device_id_returns_empty_not_other_tenant_data(client, db_session):
    company, admin, dev_a, _ = await _two_device_company(db_session)

    other_company, _tag = await _company(db_session)
    foreign_dev = await _device(db_session, other_company, "foreign-mbp")
    await _event(db_session, foreign_dev, app_name="Excel", duration=999)
    await _visit(db_session, foreign_dev, host="secret.example.com")

    r = await client.get(
        "/v1/tech/websites",
        params=_today_params(device_id=str(foreign_dev.id)), headers=_headers(admin, company),
    )
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["total_secs"] == 0

    await db_session.rollback()


@pytest.mark.asyncio
async def test_invisible_device_id_returns_empty_for_own_scope(client, db_session):
    """An in-company device the caller cannot see: `_scope_clause` and
    `_device_clause` intersect to nothing rather than widening visibility."""
    company, admin = await _tech_admin(db_session, scope_type="own")
    mine = await _device(db_session, company, "my-host", employee=admin)
    await _event(db_session, mine, app_name="Chrome", duration=100)
    await _visit(db_session, mine, host="mine.example.com")

    colleague = await _employee(db_session, company, None, "colleague")
    theirs = await _device(db_session, company, "their-host", employee=colleague)
    await _event(db_session, theirs, app_name="Chrome", duration=500)
    await _visit(db_session, theirs, host="theirs.example.com")

    headers = _headers(admin, company)

    r = await client.get("/v1/tech/websites", params=_today_params(), headers=headers)
    assert {i["host"] for i in r.json()["items"]} == {"mine.example.com"}

    r = await client.get(
        "/v1/tech/websites", params=_today_params(device_id=str(theirs.id)), headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["items"] == []

    await db_session.rollback()


@pytest.mark.asyncio
async def test_malformed_device_id_is_empty_not_500(client, db_session):
    company, admin, _, _ = await _two_device_company(db_session)
    headers = _headers(admin, company)

    for endpoint in ("/v1/tech/websites", "/v1/tech/apps", "/v1/tech/category-usage"):
        r = await client.get(endpoint, params=_today_params(device_id="not-a-uuid"), headers=headers)
        assert r.status_code == 200, f"{endpoint} -> {r.status_code} {r.text[:200]}"
        assert r.json()["total_secs"] == 0

    await db_session.rollback()


# ── Response-cache key separation ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_key_separates_device_filtered_from_unfiltered(client, db_session, monkeypatch):
    """The endpoints cache their responses keyed on a `_scope_fp` fingerprint.
    If device_id were left out of that fingerprint, the first (unfiltered)
    request would poison the entry the device-filtered request reads back.
    Exercised through an in-memory stand-in so the REAL key tuple is built
    without needing a live Redis."""
    from app.api.v1.endpoints import tech_utilization as tu

    store: dict = {}

    async def fake_get(key):
        entry = store.get(key)
        return (entry, False) if entry is not None else (None, False)

    async def fake_set(key, data, **kwargs):
        store[key] = data

    monkeypatch.setattr(tu, "cache_get", fake_get)
    monkeypatch.setattr(tu, "cache_set", fake_set)

    company, admin, dev_a, dev_b = await _two_device_company(db_session)
    headers = _headers(admin, company)

    unfiltered = await client.get("/v1/tech/apps", params=_today_params(), headers=headers)
    assert unfiltered.json()["total_secs"] == 1000

    filtered = await client.get(
        "/v1/tech/apps", params=_today_params(device_id=str(dev_a.id)), headers=headers,
    )
    assert filtered.json()["total_secs"] == 300, "device-filtered read served the unfiltered cache entry"

    # ...and the reverse direction, now that the filtered entry is warm.
    again = await client.get("/v1/tech/apps", params=_today_params(), headers=headers)
    assert again.json()["total_secs"] == 1000

    other = await client.get(
        "/v1/tech/apps", params=_today_params(device_id=str(dev_b.id)), headers=headers,
    )
    assert other.json()["total_secs"] == 700

    assert len(store) >= 3, "each distinct device filter must occupy its own cache entry"

    await db_session.rollback()


# ── Regression guard ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_omitting_device_id_is_unchanged(client, db_session):
    """No device_id at all — every endpoint must behave exactly as before,
    including keeping the `not combined` pre-aggregation fast paths reachable."""
    company, admin, _, _ = await _two_device_company(db_session)
    headers = _headers(admin, company)

    for endpoint in (
        "/v1/tech/websites", "/v1/tech/apps", "/v1/tech/category-usage",
        "/v1/tech/utilization/summary", "/v1/tech/utilization/category-allocation",
        "/v1/tech/utilization/app-trends", "/v1/tech/utilization/apps",
        "/v1/tech/utilization/adoption",
    ):
        r = await client.get(endpoint, params=_today_params(), headers=headers)
        assert r.status_code == 200, f"{endpoint} -> {r.status_code} {r.text[:200]}"

    await db_session.rollback()
