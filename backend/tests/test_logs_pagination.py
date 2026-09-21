"""GET /v1/logs — pagination correctness (PRD-2960).

Covers: stable deterministic ordering (timestamp DESC, id DESC tiebreaker),
no duplicate/missing rows across pages, offset respected, has_more semantics,
and both query shapes — the flat single-device path (device_id given) and the
fleet-wide per-device-fairness LATERAL path (no device_id).

Same fixture pattern as test_impersonation.py: live Postgres, a never-committed
session (rolled back by the `db_session`/`client` fixtures in conftest.py), and
uuid-tagged fixtures so parallel test runs never collide.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Company, Employee, Role, RolePermission, Device, ActivityEvent


# ── Fixture helpers (mirrors test_impersonation.py) ────────────────────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"Logs-{tag}", enrollment_token=f"tok-{tag}")
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


async def _device(db, company, hostname, *, employee_id=None, last_seen=None):
    d = Device(
        company_id=company.id, hostname=hostname, platform="macos",
        employee_id=employee_id, is_active=True,
        last_seen=last_seen or datetime.now(timezone.utc),
    )
    db.add(d)
    await db.flush()
    return d


async def _events(db, device, n, *, start=None, step_secs=1, event_type="window_change"):
    """Insert `n` events on `device`, newest last in insertion order but each
    `step_secs` apart so timestamp ordering is unambiguous without relying on
    insertion order."""
    base = start or (datetime.now(timezone.utc) - timedelta(hours=1))
    created = []
    for i in range(n):
        ev = ActivityEvent(
            device_id=device.id, event_type=event_type,
            app_name=f"App{i}", window_title=f"Win{i}",
            duration=5, timestamp=base + timedelta(seconds=i * step_secs),
        )
        db.add(ev)
        created.append(ev)
    await db.flush()
    return created


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _admin_headers(emp, company):
    from app.core.security import create_admin_token
    return _auth(create_admin_token(str(emp.id), emp.role, str(company.id)))


async def _all_scope_admin(db):
    company, tag = await _company(db)
    role = await _role(db, company, "Owner", scope_type="all", is_system=True)
    admin = await _employee(db, company, role, "owner")
    return company, admin


# ── Flat path (device_id given) ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_scoped_pagination_no_duplicates_or_gaps(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "flat-host")
    events = await _events(db_session, device, 25)
    headers = _admin_headers(admin, company)

    seen_ids: list[str] = []
    offset = 0
    page_size = 7
    for _ in range(10):  # generous upper bound on page count
        r = await client.get(
            f"/v1/logs?device_id={device.id}&limit={page_size}&offset={offset}",
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["offset"] == offset and body["limit"] == page_size
        items = body["items"]
        seen_ids.extend(i["id"] for i in items)
        if not body["has_more"]:
            assert len(items) <= page_size
            break
        assert len(items) == page_size
        offset += page_size
    else:
        pytest.fail("pagination did not terminate within the expected number of pages")

    # No duplicates across pages, and every inserted event was returned exactly once.
    assert len(seen_ids) == len(set(seen_ids)) == len(events)
    assert set(seen_ids) == {str(e.id) for e in events}


@pytest.mark.asyncio
async def test_device_scoped_ordering_is_deterministic_desc(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "order-host")
    await _events(db_session, device, 12)
    headers = _admin_headers(admin, company)

    r = await client.get(f"/v1/logs?device_id={device.id}&limit=50", headers=headers)
    assert r.status_code == 200
    items = r.json()["items"]
    timestamps = [i["timestamp_unix"] for i in items]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_offset_beyond_available_rows_returns_empty_not_error(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "empty-tail-host")
    await _events(db_session, device, 3)
    headers = _admin_headers(admin, company)

    r = await client.get(f"/v1/logs?device_id={device.id}&limit=10&offset=1000", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["has_more"] is False


# ── Fleet-wide path (no device_id — LATERAL per-device merge) ─────────────────

@pytest.mark.asyncio
async def test_fleet_pagination_no_duplicates_across_devices(client, db_session):
    """A busy device and a quiet device both have events; paging through the
    fleet-wide feed must eventually surface every event from both, with no
    row repeated on two pages."""
    company, admin = await _all_scope_admin(db_session)
    busy  = await _device(db_session, company, "busy-host")
    quiet = await _device(db_session, company, "quiet-host")
    busy_events  = await _events(db_session, busy, 30, step_secs=2)
    quiet_events = await _events(db_session, quiet, 5, step_secs=2)
    headers = _admin_headers(admin, company)

    all_ids = {str(e.id) for e in busy_events} | {str(e.id) for e in quiet_events}
    seen_ids: list[str] = []
    offset = 0
    page_size = 6
    for _ in range(20):
        r = await client.get(f"/v1/logs?limit={page_size}&offset={offset}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"]
        seen_ids.extend(i["id"] for i in items)
        if not body["has_more"]:
            break
        offset += page_size
    else:
        pytest.fail("fleet pagination did not terminate within the expected number of pages")

    assert len(seen_ids) == len(set(seen_ids)), "a row appeared on two pages"
    assert set(seen_ids) == all_ids, "pagination silently dropped events"


@pytest.mark.asyncio
async def test_fleet_pagination_stable_ordering_across_pages(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    d1 = await _device(db_session, company, "host-1")
    d2 = await _device(db_session, company, "host-2")
    await _events(db_session, d1, 15, step_secs=3)
    await _events(db_session, d2, 15, step_secs=3)
    headers = _admin_headers(admin, company)

    page1 = await client.get("/v1/logs?limit=10&offset=0", headers=headers)
    page2 = await client.get("/v1/logs?limit=10&offset=10", headers=headers)
    assert page1.status_code == page2.status_code == 200

    ids1 = [i["id"] for i in page1.json()["items"]]
    ids2 = [i["id"] for i in page2.json()["items"]]
    assert not (set(ids1) & set(ids2)), "same row returned on both pages"

    last_of_page1 = page1.json()["items"][-1]["timestamp_unix"]
    first_of_page2 = page2.json()["items"][0]["timestamp_unix"]
    assert last_of_page1 >= first_of_page2, "pages are not in a single consistent recency order"


@pytest.mark.asyncio
async def test_explicit_per_device_fairness_knob_still_honored(client, db_session):
    """An explicit per_device still caps a single device's contribution — the
    pre-existing fairness knob is unchanged when the caller opts into it."""
    company, admin = await _all_scope_admin(db_session)
    busy  = await _device(db_session, company, "busy-host-2")
    quiet = await _device(db_session, company, "quiet-host-2")
    await _events(db_session, busy, 20, step_secs=1)
    await _events(db_session, quiet, 2, step_secs=1)
    headers = _admin_headers(admin, company)

    r = await client.get("/v1/logs?limit=50&per_device=2", headers=headers)
    assert r.status_code == 200
    items = r.json()["items"]
    by_host: dict[str, int] = {}
    for i in items:
        by_host[i["hostname"]] = by_host.get(i["hostname"], 0) + 1
    assert by_host.get("busy-host-2", 0) <= 2
    assert by_host.get("quiet-host-2", 0) >= 1


@pytest.mark.asyncio
async def test_scoped_role_cannot_page_beyond_visible_devices(client, db_session):
    """A group-scoped viewer only ever sees events from devices in scope, no
    matter how deep they page — visible_device_ids() filtering still applies
    under pagination."""
    company, admin = await _all_scope_admin(db_session)
    visible_device = await _device(db_session, company, "visible-host")
    hidden_device  = await _device(db_session, company, "hidden-host")
    await _events(db_session, visible_device, 8, step_secs=1)
    await _events(db_session, hidden_device, 8, step_secs=1)

    own_role = await _role(
        db_session, company, "OwnScoped", scope_type="own",
        perms=[("logs", "view")],
    )
    viewer = await _employee(db_session, company, own_role, "viewer")
    viewer.role_id = own_role.id
    visible_device.employee_id = viewer.id
    await db_session.flush()

    headers = _admin_headers(viewer, company)
    seen_hosts: set[str] = set()
    offset = 0
    for _ in range(10):
        r = await client.get(f"/v1/logs?limit=3&offset={offset}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        seen_hosts.update(i["hostname"] for i in body["items"])
        if not body["has_more"]:
            break
        offset += 3

    assert seen_hosts == {"visible-host"}
