"""GET /v1/logs — server-side free-text search (search-scope regression fix).

The search box used to filter only the already-fetched page client-side. This
suite locks in the server-side replacement: `search` must narrow the WHOLE
matching range in SQL, BEFORE offset/limit pages through it — not just
whatever rows happen to be on the current page.

Fixture pattern mirrors test_logs_pagination.py (kept self-contained here
rather than imported, since these two files are edited independently).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Company, Employee, Role, RolePermission, Device, ActivityEvent


# ── Fixture helpers (mirrors test_logs_pagination.py) ──────────────────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"LogsSearch-{tag}", enrollment_token=f"tok-{tag}")
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


def _event(device, *, event_type="window_change", app_name=None, window_title=None,
           url=None, timestamp=None, duration=5):
    return ActivityEvent(
        device_id=device.id, event_type=event_type, app_name=app_name,
        window_title=window_title, url=url, duration=duration,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


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


# ── Matches on each searchable column ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_matches_app_name(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "app-search-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    target = _event(device, app_name="UniqueAppXYZ", window_title="w", timestamp=now)
    noise = _event(device, app_name="SomethingElse", window_title="w", timestamp=now - timedelta(seconds=1))
    db_session.add_all([target, noise])
    await db_session.flush()

    r = await client.get("/v1/logs?limit=50&search=UniqueAppXYZ", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert str(target.id) in ids
    assert str(noise.id) not in ids


@pytest.mark.asyncio
async def test_search_matches_window_title(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "title-search-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    target = _event(device, app_name="a", window_title="UniqueTitleXYZ", timestamp=now)
    noise = _event(device, app_name="a", window_title="Nothing", timestamp=now - timedelta(seconds=1))
    db_session.add_all([target, noise])
    await db_session.flush()

    r = await client.get("/v1/logs?limit=50&search=UniqueTitleXYZ", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert str(target.id) in ids
    assert str(noise.id) not in ids


@pytest.mark.asyncio
async def test_search_matches_url(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "url-search-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    target = _event(device, event_type="url_visit", url="https://unique-url-xyz.example.com", timestamp=now)
    noise = _event(device, event_type="url_visit", url="https://unrelated.example.com", timestamp=now - timedelta(seconds=1))
    db_session.add_all([target, noise])
    await db_session.flush()

    r = await client.get("/v1/logs?limit=50&search=unique-url-xyz", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert str(target.id) in ids
    assert str(noise.id) not in ids


@pytest.mark.asyncio
async def test_search_matches_employee_name(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    employee = await _employee(db_session, company, None, "SearchableEmployeeXYZ")
    target_device = await _device(db_session, company, "employee-search-host", employee_id=employee.id)
    other_device = await _device(db_session, company, "other-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    target = _event(target_device, app_name="a", window_title="w", timestamp=now)
    noise = _event(other_device, app_name="a", window_title="w", timestamp=now - timedelta(seconds=1))
    db_session.add_all([target, noise])
    await db_session.flush()

    r = await client.get("/v1/logs?limit=50&search=SearchableEmployeeXYZ", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert str(target.id) in ids
    assert str(noise.id) not in ids


@pytest.mark.asyncio
async def test_search_matches_hostname(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    target_device = await _device(db_session, company, "findable-hostname-xyz")
    other_device = await _device(db_session, company, "other-hostname")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    target = _event(target_device, app_name="a", window_title="w", timestamp=now)
    noise = _event(other_device, app_name="a", window_title="w", timestamp=now - timedelta(seconds=1))
    db_session.add_all([target, noise])
    await db_session.flush()

    r = await client.get("/v1/logs?limit=50&search=findable-hostname-xyz", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert str(target.id) in ids
    assert str(noise.id) not in ids


# ── THE critical regression test ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_survives_per_device_recency_cap(client, db_session):
    """The bug this task exists to prevent: on the fleet-wide (no device_id)
    path, each device's LATERAL sub-query is capped to its `per_device` most
    recent events BEFORE the outer merge/page. If `search` were applied AFTER
    that cap (filtering the merged result) instead of INSIDE the LATERAL's own
    WHERE, an older matching event on a busy device would be discarded by the
    recency cap before search ever got a chance to see it — search would
    silently miss a real match.

    `LOGS_PER_DEVICE_DEFAULT` is 10, and with offset=0/limit=5,
    `_pagination_per_device` computes max(10, 0+5+1) = 10. So: seed MORE than
    10 non-matching, recent events (enough to fully occupy the LATERAL's
    LIMIT 10 on recency alone), then ONE matching event further back in time.
    If search is (incorrectly) applied post-cap, the older matching row never
    makes it into the per-device candidate pool and the search returns empty.
    If search is (correctly) applied inside the LATERAL's WHERE, the pool is
    built from MATCHING events only, so the single match trivially survives.
    """
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "busy-search-host")
    headers = _admin_headers(admin, company)

    per_device_cap = 10  # LOGS_PER_DEVICE_DEFAULT
    now = datetime.now(timezone.utc)

    # Recent, NON-matching noise — more than per_device_cap of them, so a
    # recency-only LIMIT 10 (search applied after) would be entirely noise.
    noise_events = []
    for i in range(per_device_cap + 5):
        ev = _event(
            device, app_name="NoiseApp", window_title="nothing interesting",
            timestamp=now - timedelta(seconds=i),  # all newer than the target below
        )
        db_session.add(ev)
        noise_events.append(ev)

    # The single OLDER matching event, well behind all the noise.
    target = _event(
        device, app_name="UniqueSearchTargetXYZ", window_title="the one we want",
        timestamp=now - timedelta(hours=2),
    )
    db_session.add(target)
    await db_session.flush()

    r = await client.get(
        "/v1/logs?limit=5&offset=0&search=UniqueSearchTargetXYZ", headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [i["id"] for i in body["items"]]
    assert str(target.id) in ids, (
        "search did not survive the per-device recency cap — it is being "
        "applied AFTER the LATERAL's LIMIT instead of inside its WHERE"
    )
    # And none of the non-matching noise should be returned.
    assert not (set(ids) & {str(e.id) for e in noise_events})


# ── Search + pagination compose correctly ──────────────────────────────────────

@pytest.mark.asyncio
async def test_search_pagination_no_duplicates_or_gaps(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "search-page-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    matching = []
    for i in range(23):
        ev = _event(device, app_name="Zephyr", window_title=f"Win{i}", timestamp=now - timedelta(seconds=i))
        db_session.add(ev)
        matching.append(ev)
    for i in range(10):
        ev = _event(device, app_name="OtherApp", window_title=f"Noise{i}", timestamp=now - timedelta(seconds=100 + i))
        db_session.add(ev)
    await db_session.flush()

    seen_ids: list[str] = []
    offset = 0
    page_size = 5
    for _ in range(20):
        r = await client.get(
            f"/v1/logs?limit={page_size}&offset={offset}&search=Zephyr", headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"]
        seen_ids.extend(i["id"] for i in items)
        if not body["has_more"]:
            break
        offset += page_size
    else:
        pytest.fail("search pagination did not terminate within the expected number of pages")

    assert len(seen_ids) == len(set(seen_ids)), "a row appeared on two pages"
    assert set(seen_ids) == {str(e.id) for e in matching}, "pagination dropped or added rows"


@pytest.mark.asyncio
async def test_search_pagination_on_flat_device_scoped_path(client, db_session):
    """Same composition guarantee on the flat (device_id given) query path."""
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "search-flat-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    matching = []
    for i in range(17):
        ev = _event(device, app_name="FlatMatch", window_title=f"Win{i}", timestamp=now - timedelta(seconds=i))
        db_session.add(ev)
        matching.append(ev)
    for i in range(5):
        ev = _event(device, app_name="FlatNoise", window_title=f"Noise{i}", timestamp=now - timedelta(seconds=50 + i))
        db_session.add(ev)
    await db_session.flush()

    seen_ids: list[str] = []
    offset = 0
    page_size = 4
    for _ in range(20):
        r = await client.get(
            f"/v1/logs?device_id={device.id}&limit={page_size}&offset={offset}&search=FlatMatch",
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"]
        seen_ids.extend(i["id"] for i in items)
        if not body["has_more"]:
            break
        offset += page_size
    else:
        pytest.fail("flat-path search pagination did not terminate")

    assert len(seen_ids) == len(set(seen_ids))
    assert set(seen_ids) == {str(e.id) for e in matching}


# ── Empty search is a no-op ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_search_identical_to_no_search(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "empty-search-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    for i in range(8):
        db_session.add(_event(device, app_name=f"App{i}", window_title=f"Win{i}", timestamp=now - timedelta(seconds=i)))
    await db_session.flush()

    r1 = await client.get(f"/v1/logs?device_id={device.id}&limit=50", headers=headers)
    r2 = await client.get(f"/v1/logs?device_id={device.id}&limit=50&search=", headers=headers)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()

    # Same guarantee on the fleet-wide path.
    r3 = await client.get("/v1/logs?limit=50", headers=headers)
    r4 = await client.get("/v1/logs?limit=50&search=", headers=headers)
    assert r3.status_code == r4.status_code == 200
    assert r3.json() == r4.json()


# ── Wildcard-injection safety (delegated to build_search — confirm, don't reimplement) ──

@pytest.mark.asyncio
async def test_search_percent_literal_not_treated_as_wildcard(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "percent-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    # Contains a LITERAL '%' character.
    literal_match = _event(device, app_name="100%LiteralPercent", timestamp=now)
    # Does NOT contain '%', but would match the pattern "100" + wildcard-run +
    # "LiteralPercent" if '%' in the search term were left as a live wildcard.
    wildcard_victim = _event(device, app_name="100xLiteralPercent", timestamp=now - timedelta(seconds=1))
    db_session.add_all([literal_match, wildcard_victim])
    await db_session.flush()

    r = await client.get("/v1/logs?limit=50&search=100%25LiteralPercent", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert str(literal_match.id) in ids
    assert str(wildcard_victim.id) not in ids, "literal '%' in search term was treated as a LIKE wildcard"


@pytest.mark.asyncio
async def test_search_underscore_literal_not_treated_as_wildcard(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    device = await _device(db_session, company, "underscore-host")
    headers = _admin_headers(admin, company)

    now = datetime.now(timezone.utc)
    # Contains a LITERAL '_' character.
    literal_match = _event(device, app_name="AB_CD_Marker", timestamp=now)
    # Any single character in place of '_' — would match if '_' were left as a
    # live single-char wildcard.
    wildcard_victim = _event(device, app_name="ABXCD_Marker", timestamp=now - timedelta(seconds=1))
    db_session.add_all([literal_match, wildcard_victim])
    await db_session.flush()

    r = await client.get("/v1/logs?limit=50&search=AB_CD_Marker", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert str(literal_match.id) in ids
    assert str(wildcard_victim.id) not in ids, "literal '_' in search term was treated as a LIKE wildcard"


# ── Scoping still holds with search applied ─────────────────────────────────────

@pytest.mark.asyncio
async def test_search_respects_own_scoping(client, db_session):
    company, admin = await _all_scope_admin(db_session)
    visible_device = await _device(db_session, company, "visible-search-host")
    hidden_device = await _device(db_session, company, "hidden-search-host")

    now = datetime.now(timezone.utc)
    visible_ev = _event(visible_device, app_name="ScopeMarkerXYZ", window_title="visible", timestamp=now)
    hidden_ev = _event(hidden_device, app_name="ScopeMarkerXYZ", window_title="hidden", timestamp=now - timedelta(seconds=1))
    db_session.add_all([visible_ev, hidden_ev])
    await db_session.flush()

    own_role = await _role(
        db_session, company, "OwnScopedSearch", scope_type="own",
        perms=[("logs", "view")],
    )
    viewer = await _employee(db_session, company, own_role, "search-viewer")
    viewer.role_id = own_role.id
    visible_device.employee_id = viewer.id
    await db_session.flush()

    headers = _admin_headers(viewer, company)
    r = await client.get("/v1/logs?limit=50&search=ScopeMarkerXYZ", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    hostnames = {i["hostname"] for i in r.json()["items"]}
    assert str(visible_ev.id) in ids
    assert str(hidden_ev.id) not in ids
    assert hostnames == {"visible-search-host"}


@pytest.mark.asyncio
async def test_search_respects_groups_scoping(client, db_session):
    from app.db.models import Group, GroupMember

    company, admin = await _all_scope_admin(db_session)
    visible_device = await _device(db_session, company, "group-visible-host")
    hidden_device = await _device(db_session, company, "group-hidden-host")

    now = datetime.now(timezone.utc)
    visible_ev = _event(visible_device, app_name="GroupScopeMarkerXYZ", timestamp=now)
    hidden_ev = _event(hidden_device, app_name="GroupScopeMarkerXYZ", timestamp=now - timedelta(seconds=1))
    db_session.add_all([visible_ev, hidden_ev])
    await db_session.flush()

    group = Group(company_id=company.id, name="SearchScopeGroup")
    db_session.add(group)
    await db_session.flush()
    db_session.add(GroupMember(group_id=group.id, member_type="device", device_id=visible_device.id))
    await db_session.flush()

    groups_role = await _role(
        db_session, company, "GroupsScopedSearch", scope_type="groups",
        perms=[("logs", "view")],
    )
    viewer = await _employee(db_session, company, groups_role, "group-search-viewer")
    from app.db.models import EmployeeGroupAccess
    db_session.add(EmployeeGroupAccess(employee_id=viewer.id, group_id=group.id))
    await db_session.flush()

    headers = _admin_headers(viewer, company)
    r = await client.get("/v1/logs?limit=50&search=GroupScopeMarkerXYZ", headers=headers)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    hostnames = {i["hostname"] for i in r.json()["items"]}
    assert str(visible_ev.id) in ids
    assert str(hidden_ev.id) not in ids
    assert hostnames == {"group-visible-host"}
