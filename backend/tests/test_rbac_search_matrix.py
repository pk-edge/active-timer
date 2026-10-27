"""Role/permission matrix verification for the PRD-2960 search + logs-pagination
rework (backend/app/core/search.py, GET /v1/logs, GET /v1/users(+impersonatable),
GET /v1/tech/filters/users, GET /v1/audit, GET /v1/admin/files).

This is a VERIFICATION pass, not a feature test: the goal is to build confidence
that the rework did not (a) leak data across the scope_type=all/groups/own axis,
(b) leak data across companies, (c) loosen any (module, action) gate, or
(d) reintroduce wildcard-injection via the shared search helper.

Fixture pattern mirrors test_impersonation.py / test_logs_pagination.py / test_search.py:
live Postgres, a never-committed session (rolled back by conftest's db_session/client
fixtures), uuid.uuid4().hex[:8]-tagged names so parallel runs never collide.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import create_admin_token
from app.db.models import (
    Company, Employee, Role, RolePermission, Device, ActivityEvent,
    Group, GroupMember, EmployeeGroupAccess, AuditLog, SharedFile,
)


# ── Fixture helpers ─────────────────────────────────────────────────────────────

async def _company(db, label="Co"):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"{label}-{tag}", enrollment_token=f"tok-{tag}")
    db.add(c)
    await db.flush()
    return c, tag


async def _role(db, company, name, *, scope_type="all", is_system=False, perms=()):
    tag = uuid.uuid4().hex[:6]
    r = Role(company_id=company.id, name=f"{name}-{tag}", scope_type=scope_type, is_system=is_system)
    db.add(r)
    await db.flush()
    for module, action in perms:
        db.add(RolePermission(role_id=r.id, module=module, action=action))
    await db.flush()
    return r


async def _employee(db, company, role, label, *, is_active=True, is_approved=True):
    tag = uuid.uuid4().hex[:8]
    e = Employee(
        company_id=company.id, email=f"{label}-{tag}@test.io", name=f"{label} {tag}",
        role_id=role.id if role else None, is_active=is_active, is_approved=is_approved,
        password_hash="x",
    )
    db.add(e)
    await db.flush()
    return e


async def _device(db, company, hostname, *, employee_id=None, last_seen=None):
    tag = uuid.uuid4().hex[:6]
    d = Device(
        company_id=company.id, hostname=f"{hostname}-{tag}", platform="macos",
        employee_id=employee_id, is_active=True,
        last_seen=last_seen or datetime.now(timezone.utc),
    )
    db.add(d)
    await db.flush()
    return d


async def _events(db, device, n, *, start=None, step_secs=1, event_type="window_change"):
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


async def _group(db, company, name, *, parent=None):
    g = Group(company_id=company.id, name=name, parent_id=parent.id if parent else None)
    db.add(g)
    await db.flush()
    return g


async def _add_employee_to_group(db, group, employee):
    db.add(GroupMember(group_id=group.id, member_type="employee", employee_id=employee.id))
    await db.flush()


async def _add_device_to_group(db, group, device):
    db.add(GroupMember(group_id=group.id, member_type="device", device_id=device.id))
    await db.flush()


async def _grant_group_access(db, employee, group):
    db.add(EmployeeGroupAccess(employee_id=employee.id, group_id=group.id))
    await db.flush()


def _headers(emp, company):
    return {"Authorization": f"Bearer {create_admin_token(str(emp.id), emp.role, str(company.id))}"}


async def _super_admin(db, label="SuperAdmin"):
    company, tag = await _company(db, label)
    role = await _role(db, company, "SuperAdmin", scope_type="all", is_system=True)
    admin = await _employee(db, company, role, "super")
    return company, admin


# ── Role builders for the matrix ─────────────────────────────────────────────────

async def _logs_only_all_scope_role(db, company):
    """Config #2: all-scope, ONLY logs:view — no users/devices/tech."""
    return await _role(db, company, "LogsOnlyAll", scope_type="all", is_system=False,
                        perms=[("logs", "view")])


async def _groups_scope_role_logs_tech(db, company):
    """Config #3: groups-scope, logs:view + tech:view, no users:view."""
    return await _role(db, company, "GroupsLogsTech", scope_type="groups", is_system=False,
                        perms=[("logs", "view"), ("tech", "view")])


async def _own_scope_role_minimal(db, company):
    """Config #4: own-scope (Talent-like), minimal perms: logs:view + tech:view."""
    return await _role(db, company, "OwnMinimal", scope_type="own", is_system=False,
                        perms=[("logs", "view"), ("tech", "view")])


async def _users_only_role(db, company):
    """Config #5: users:view only — must be refused everywhere else."""
    return await _role(db, company, "UsersOnly", scope_type="all", is_system=False,
                        perms=[("users", "view")])


# =============================================================================
# 1. GET /v1/logs
# =============================================================================

@pytest.mark.asyncio
async def test_logs_response_envelope_shape_for_all_roles(client, db_session):
    company, admin = await _super_admin(db_session)
    device = await _device(db_session, company, "envelope-host")
    await _events(db_session, device, 3)

    r = await client.get("/v1/logs", headers=_headers(admin, company))
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"items", "has_more", "offset", "limit"}
    assert isinstance(body["items"], list)
    assert isinstance(body["has_more"], bool)


@pytest.mark.asyncio
async def test_logs_offset_beyond_cap_is_rejected_422(client, db_session):
    company, admin = await _super_admin(db_session)
    r = await client.get("/v1/logs?offset=20001", headers=_headers(admin, company))
    assert r.status_code == 422
    # boundary itself must be accepted (le=20_000)
    r2 = await client.get("/v1/logs?offset=20000", headers=_headers(admin, company))
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_logs_requires_logs_view_permission(client, db_session):
    """Config #5: users:view only must be 403'd on /v1/logs (gate unchanged)."""
    company, tag = await _company(db_session)
    role = await _users_only_role(db_session, company)
    user = await _employee(db_session, company, role, "usersonly")

    r = await client.get("/v1/logs", headers=_headers(user, company))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_logs_all_scope_custom_role_with_only_logs_view_sees_whole_company(client, db_session):
    """Config #2: the exact shape of the original bug — all-scope role holding
    ONLY logs:view must still see every device's events (permission and data
    scope are independent axes)."""
    company, tag = await _company(db_session)
    role = await _logs_only_all_scope_role(db_session, company)
    caller = await _employee(db_session, company, role, "logsonly")
    d1 = await _device(db_session, company, "host-a")
    d2 = await _device(db_session, company, "host-b")
    await _events(db_session, d1, 4, step_secs=5)
    await _events(db_session, d2, 4, step_secs=5)

    r = await client.get("/v1/logs?limit=50", headers=_headers(caller, company))
    assert r.status_code == 200
    hosts = {i["hostname"] for i in r.json()["items"]}
    assert d1.hostname in hosts and d2.hostname in hosts


@pytest.mark.asyncio
async def test_logs_groups_scope_restricted_to_own_group_devices(client, db_session):
    """Config #3: groups-scope caller must never see a device outside its
    assigned group, even though it holds logs:view."""
    company, tag = await _company(db_session)
    role = await _groups_scope_role_logs_tech(db_session, company)
    caller = await _employee(db_session, company, role, "groupscaller")

    group = await _group(db_session, company, "G1")
    await _grant_group_access(db_session, caller, group)

    in_group_device = await _device(db_session, company, "in-group-host")
    outside_device = await _device(db_session, company, "outside-host")
    await _add_device_to_group(db_session, group, in_group_device)

    await _events(db_session, in_group_device, 5, step_secs=2)
    await _events(db_session, outside_device, 5, step_secs=2)

    r = await client.get("/v1/logs?limit=50", headers=_headers(caller, company))
    assert r.status_code == 200
    hosts = {i["hostname"] for i in r.json()["items"]}
    assert in_group_device.hostname in hosts
    assert outside_device.hostname not in hosts


@pytest.mark.asyncio
async def test_logs_own_scope_sees_only_own_devices(client, db_session):
    """Config #4: own-scope caller sees only devices assigned to itself."""
    company, tag = await _company(db_session)
    role = await _own_scope_role_minimal(db_session, company)
    caller = await _employee(db_session, company, role, "talent")
    other = await _employee(db_session, company, role, "talent-other")

    my_device = await _device(db_session, company, "my-host", employee_id=caller.id)
    other_device = await _device(db_session, company, "other-host", employee_id=other.id)
    await _events(db_session, my_device, 4, step_secs=2)
    await _events(db_session, other_device, 4, step_secs=2)

    r = await client.get("/v1/logs?limit=50", headers=_headers(caller, company))
    assert r.status_code == 200
    hosts = {i["hostname"] for i in r.json()["items"]}
    assert hosts == {my_device.hostname}


@pytest.mark.asyncio
async def test_logs_pagination_params_work_per_role(client, db_session):
    company, tag = await _company(db_session)
    role = await _logs_only_all_scope_role(db_session, company)
    caller = await _employee(db_session, company, role, "pager")
    device = await _device(db_session, company, "pager-host")
    await _events(db_session, device, 10, step_secs=1)

    r1 = await client.get(f"/v1/logs?device_id={device.id}&limit=4&offset=0", headers=_headers(caller, company))
    r2 = await client.get(f"/v1/logs?device_id={device.id}&limit=4&offset=4", headers=_headers(caller, company))
    assert r1.status_code == r2.status_code == 200
    ids1 = {i["id"] for i in r1.json()["items"]}
    ids2 = {i["id"] for i in r2.json()["items"]}
    assert not (ids1 & ids2)
    assert r1.json()["offset"] == 0 and r1.json()["limit"] == 4
    assert r2.json()["offset"] == 4 and r2.json()["limit"] == 4


# =============================================================================
# 2. GET /v1/users?search=
# =============================================================================

@pytest.mark.asyncio
async def test_users_search_requires_users_view(client, db_session):
    """Config #2: all-scope role with only logs:view must be 403'd on /v1/users."""
    company, tag = await _company(db_session)
    role = await _logs_only_all_scope_role(db_session, company)
    caller = await _employee(db_session, company, role, "logsonly")

    r = await client.get("/v1/users?search=anything", headers=_headers(caller, company))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_users_search_groups_scope_role_without_users_view_is_403(client, db_session):
    """Config #3: groups-scope role holds logs:view + tech:view but NOT
    users:view — must be refused on /v1/users, same as any other missing gate."""
    company, tag = await _company(db_session)
    role = await _groups_scope_role_logs_tech(db_session, company)
    caller = await _employee(db_session, company, role, "groupscaller2")

    r = await client.get("/v1/users?search=anything", headers=_headers(caller, company))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_users_search_groups_scope_restricted_to_own_group(client, db_session):
    """A groups-scoped caller with users:view only ever sees employees in its
    assigned group (+ subtree), even when the search term matches an outsider."""
    company, tag = await _company(db_session)
    role = await _role(
        db_session, company, "GroupsUsers", scope_type="groups",
        perms=[("users", "view")],
    )
    caller = await _employee(db_session, company, role, "grpcaller")

    group = await _group(db_session, company, "GU1")
    await _grant_group_access(db_session, caller, group)

    insider = await _employee(db_session, company, None, "FindableInsider")
    outsider = await _employee(db_session, company, None, "FindableOutsider")
    await _add_employee_to_group(db_session, group, insider)

    r = await client.get("/v1/users?search=Findable", headers=_headers(caller, company))
    assert r.status_code == 200
    names = {u["name"] for u in r.json()}
    assert insider.name in names
    assert outsider.name not in names


@pytest.mark.asyncio
async def test_users_search_escaping_percent_does_not_match_everything(client, db_session):
    company, tag = await _company(db_session)
    role = await _users_only_role(db_session, company)
    caller = await _employee(db_session, company, role, "usersonly")
    await _employee(db_session, company, None, "SomeoneElse")

    r = await client.get("/v1/users?search=%25", headers=_headers(caller, company))
    assert r.status_code == 200
    # literal '%' matches none of the seeded names/emails
    names = {u["name"] for u in r.json()}
    assert caller.name not in names and "SomeoneElse" not in " ".join(names)


# =============================================================================
# 3. GET /v1/users/impersonatable
# =============================================================================

@pytest.mark.asyncio
async def test_impersonatable_only_super_admin_all_other_roles_403_generic(client, db_session):
    company, tag = await _company(db_session)

    logs_only = await _employee(db_session, company, await _logs_only_all_scope_role(db_session, company), "logsonly")
    groups_role = await _employee(db_session, company, await _groups_scope_role_logs_tech(db_session, company), "groupsrole")
    own_role = await _employee(db_session, company, await _own_scope_role_minimal(db_session, company), "ownrole")
    users_only = await _employee(db_session, company, await _users_only_role(db_session, company), "usersonly")

    for caller in (logs_only, groups_role, own_role, users_only):
        r = await client.get("/v1/users/impersonatable?search=a", headers=_headers(caller, company))
        assert r.status_code == 403, f"{caller.name} should be refused"
        assert r.json()["detail"] == "Not found", "detail must stay generic, not leak feature existence"


@pytest.mark.asyncio
async def test_impersonatable_super_admin_can_search(client, db_session):
    company, admin = await _super_admin(db_session)
    target = await _employee(db_session, company, None, "ImpTargetFindme")

    r = await client.get("/v1/users/impersonatable?search=ImpTargetFindme", headers=_headers(admin, company))
    assert r.status_code == 200
    names = {u["name"] for u in r.json()}
    assert target.name in names


@pytest.mark.asyncio
async def test_impersonatable_search_escaping_wildcard(client, db_session):
    company, admin = await _super_admin(db_session)
    await _employee(db_session, company, None, "WildcardVictim")

    r = await client.get("/v1/users/impersonatable?search=%25", headers=_headers(admin, company))
    assert r.status_code == 200
    # a literal '%' must not return every candidate
    assert "WildcardVictim" not in {u["name"] for u in r.json()}


# =============================================================================
# 4. GET /v1/tech/filters/users
# =============================================================================

@pytest.mark.asyncio
async def test_tech_filters_users_requires_tech_view_not_logs_view(client, db_session):
    """A role with logs:view but NOT tech:view must 403 here."""
    company, tag = await _company(db_session)
    role = await _logs_only_all_scope_role(db_session, company)  # logs:view only
    caller = await _employee(db_session, company, role, "logsonly")

    r = await client.get("/v1/tech/filters/users", headers=_headers(caller, company))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_tech_filters_users_own_scope_returns_empty(client, db_session):
    company, tag = await _company(db_session)
    role = await _own_scope_role_minimal(db_session, company)  # has tech:view
    caller = await _employee(db_session, company, role, "talent")

    r = await client.get("/v1/tech/filters/users", headers=_headers(caller, company))
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_tech_filters_users_groups_scope_restricted_to_group_members(client, db_session):
    company, tag = await _company(db_session)
    role = await _groups_scope_role_logs_tech(db_session, company)
    caller = await _employee(db_session, company, role, "grpcaller")

    group = await _group(db_session, company, "TG1")
    await _grant_group_access(db_session, caller, group)

    insider = await _employee(db_session, company, None, "TechInsider")
    outsider = await _employee(db_session, company, None, "TechOutsider")
    await _add_employee_to_group(db_session, group, insider)

    r = await client.get("/v1/tech/filters/users", headers=_headers(caller, company))
    assert r.status_code == 200
    names = {u["name"] for u in r.json()}
    assert insider.name in names
    assert outsider.name not in names


@pytest.mark.asyncio
async def test_tech_filters_users_all_scope_sees_everyone(client, db_session):
    company, admin = await _super_admin(db_session)
    e1 = await _employee(db_session, company, None, "AllScopeVisible1")
    e2 = await _employee(db_session, company, None, "AllScopeVisible2")

    r = await client.get("/v1/tech/filters/users", headers=_headers(admin, company))
    assert r.status_code == 200
    names = {u["name"] for u in r.json()}
    assert e1.name in names and e2.name in names


# =============================================================================
# 5. GET /v1/audit?search=
# =============================================================================

@pytest.mark.asyncio
async def test_audit_requires_audit_view(client, db_session):
    company, tag = await _company(db_session)
    role = await _users_only_role(db_session, company)
    caller = await _employee(db_session, company, role, "usersonly")

    r = await client.get("/v1/audit", headers=_headers(caller, company))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_audit_search_percent_does_not_wildcard_match(client, db_session):
    company, tag = await _company(db_session)
    role = await _role(db_session, company, "AuditRole", scope_type="all", perms=[("audit", "view")])
    caller = await _employee(db_session, company, role, "auditor")

    db_session.add(AuditLog(
        company_id=company.id, actor_employee_id=caller.id, actor_role="auditor",
        action=f"user.create-{tag}", target_type="user", target_id="t1",
    ))
    await db_session.flush()

    r = await client.get("/v1/audit?search=%25", headers=_headers(caller, company))
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_audit_search_underscore_and_quote_do_not_500(client, db_session):
    company, tag = await _company(db_session)
    role = await _role(db_session, company, "AuditRole2", scope_type="all", perms=[("audit", "view")])
    caller = await _employee(db_session, company, role, "auditor2")

    for term in ("_", "'", "a'b", "%_%"):
        r = await client.get("/v1/audit", params={"search": term}, headers=_headers(caller, company))
        assert r.status_code == 200, f"term={term!r} caused {r.status_code}: {r.text}"


# =============================================================================
# 6. GET /v1/admin/files?q=
# =============================================================================

@pytest.mark.asyncio
async def test_files_requires_files_view(client, db_session):
    company, tag = await _company(db_session)
    role = await _users_only_role(db_session, company)
    caller = await _employee(db_session, company, role, "usersonly")

    r = await client.get("/v1/files", headers=_headers(caller, company))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_files_search_percent_does_not_wildcard_match(client, db_session):
    company, tag = await _company(db_session)
    role = await _role(db_session, company, "FilesRole", scope_type="all", perms=[("files", "view")])
    caller = await _employee(db_session, company, role, "filesviewer")

    db_session.add(SharedFile(
        company_id=company.id, uploaded_by=caller.id, original_filename=f"secret-{tag}.pdf",
        s3_key=f"shared-files/{company.id}/{tag}_secret.pdf", mime_type="application/pdf",
        file_size_bytes=10, public_token=uuid.uuid4().hex,
    ))
    await db_session.flush()

    r = await client.get("/v1/files?q=%25", headers=_headers(caller, company))
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_files_search_underscore_and_quote_do_not_500(client, db_session):
    company, tag = await _company(db_session)
    role = await _role(db_session, company, "FilesRole2", scope_type="all", perms=[("files", "view")])
    caller = await _employee(db_session, company, role, "filesviewer2")

    for term in ("_", "'", "a'b", "%_%"):
        r = await client.get("/v1/files", params={"q": term}, headers=_headers(caller, company))
        assert r.status_code == 200, f"term={term!r} caused {r.status_code}: {r.text}"


# =============================================================================
# 7. Cross-cutting: company/tenant isolation
# =============================================================================

@pytest.mark.asyncio
async def test_users_search_never_crosses_company_boundary(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company_a, _ = await _company(db_session, "CoA")
    company_b, _ = await _company(db_session, "CoB")

    role_a = await _role(db_session, company_a, "AdminA", scope_type="all", perms=[("users", "view")])
    role_b = await _role(db_session, company_b, "AdminB", scope_type="all", perms=[("users", "view")])
    admin_a = await _employee(db_session, company_a, role_a, "adminA")
    admin_b = await _employee(db_session, company_b, role_b, "adminB")

    # Same distinguishing name-token in BOTH companies.
    emp_a = Employee(company_id=company_a.id, email=f"crosscheck-a-{tag}@test.io",
                      name=f"CrossCheck Alpha {tag}", is_active=True, is_approved=True)
    emp_b = Employee(company_id=company_b.id, email=f"crosscheck-b-{tag}@test.io",
                      name=f"CrossCheck Beta {tag}", is_active=True, is_approved=True)
    db_session.add_all([emp_a, emp_b])
    await db_session.flush()

    r = await client.get("/v1/users?search=CrossCheck", headers=_headers(admin_a, company_a))
    assert r.status_code == 200
    names = {u["name"] for u in r.json()}
    assert emp_a.name in names
    assert emp_b.name not in names, "search leaked an employee from another company"


@pytest.mark.asyncio
async def test_impersonatable_search_never_crosses_company_boundary(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company_a, admin_a = await _super_admin(db_session, "ImpCoA")
    company_b, admin_b = await _super_admin(db_session, "ImpCoB")

    emp_a = Employee(company_id=company_a.id, email=f"imp-cross-a-{tag}@test.io",
                      name=f"ImpCross Alpha {tag}", is_active=True, is_approved=True)
    emp_b = Employee(company_id=company_b.id, email=f"imp-cross-b-{tag}@test.io",
                      name=f"ImpCross Beta {tag}", is_active=True, is_approved=True)
    db_session.add_all([emp_a, emp_b])
    await db_session.flush()

    r = await client.get("/v1/users/impersonatable?search=ImpCross", headers=_headers(admin_a, company_a))
    assert r.status_code == 200
    names = {u["name"] for u in r.json()}
    assert emp_a.name in names
    assert emp_b.name not in names


@pytest.mark.asyncio
async def test_audit_search_never_crosses_company_boundary(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company_a, _ = await _company(db_session, "AudCoA")
    company_b, _ = await _company(db_session, "AudCoB")
    role_a = await _role(db_session, company_a, "AuditA", scope_type="all", perms=[("audit", "view")])
    admin_a = await _employee(db_session, company_a, role_a, "auditadminA")
    emp_b = await _employee(db_session, company_b, None, "OtherCoActor")

    db_session.add_all([
        AuditLog(company_id=company_a.id, actor_employee_id=admin_a.id, actor_role="admin",
                 action=f"crossaudit-a-{tag}", target_type="user", target_id="1"),
        AuditLog(company_id=company_b.id, actor_employee_id=emp_b.id, actor_role="admin",
                 action=f"crossaudit-b-{tag}", target_type="user", target_id="2"),
    ])
    await db_session.flush()

    r = await client.get("/v1/audit?search=crossaudit", headers=_headers(admin_a, company_a))
    assert r.status_code == 200
    actions = {e["action"] for e in r.json()["entries"]}
    assert f"crossaudit-a-{tag}" in actions
    assert f"crossaudit-b-{tag}" not in actions


@pytest.mark.asyncio
async def test_files_search_never_crosses_company_boundary(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company_a, _ = await _company(db_session, "FileCoA")
    company_b, _ = await _company(db_session, "FileCoB")
    role_a = await _role(db_session, company_a, "FilesA", scope_type="all", perms=[("files", "view")])
    admin_a = await _employee(db_session, company_a, role_a, "filesadminA")
    admin_b = await _employee(db_session, company_b, None, "filesadminB")

    db_session.add_all([
        SharedFile(company_id=company_a.id, uploaded_by=admin_a.id,
                   original_filename=f"crossfile-a-{tag}.pdf",
                   s3_key=f"sf/{company_a.id}/{tag}_a.pdf", mime_type="application/pdf",
                   file_size_bytes=1, public_token=uuid.uuid4().hex),
        SharedFile(company_id=company_b.id, uploaded_by=admin_b.id,
                   original_filename=f"crossfile-b-{tag}.pdf",
                   s3_key=f"sf/{company_b.id}/{tag}_b.pdf", mime_type="application/pdf",
                   file_size_bytes=1, public_token=uuid.uuid4().hex),
    ])
    await db_session.flush()

    r = await client.get("/v1/files?q=crossfile", headers=_headers(admin_a, company_a))
    assert r.status_code == 200
    filenames = {i["original_filename"] for i in r.json()["items"]}
    assert f"crossfile-a-{tag}.pdf" in filenames
    assert f"crossfile-b-{tag}.pdf" not in filenames


# =============================================================================
# 8. Cross-cutting: wildcard / injection smoke test across all four search endpoints
# =============================================================================

@pytest.mark.asyncio
async def test_wildcard_injection_smoke_all_four_search_endpoints(client, db_session):
    """% , _ , and a raw single quote must never 500 or leak a stack trace, on
    any of the four search endpoints, for a caller holding every relevant
    permission."""
    company, admin = await _super_admin(db_session, "SmokeCo")

    dangerous_terms = ["%", "_", "'", "%%", "a%b_c'd"]

    endpoints = [
        ("/v1/audit", "search"),
        ("/v1/users", "search"),
        ("/v1/users/impersonatable", "search"),
        ("/v1/files", "q"),
    ]

    for path, param in endpoints:
        for term in dangerous_terms:
            r = await client.get(path, params={param: term}, headers=_headers(admin, company))
            assert r.status_code == 200, f"{path}?{param}={term!r} -> {r.status_code}: {r.text[:300]}"
            # Sanity: response is valid JSON (already guaranteed by httpx .json()
            # not raising) and doesn't look like an unhandled traceback.
            assert "Traceback" not in r.text
