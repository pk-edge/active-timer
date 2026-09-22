"""employee_group_access must stay consistent with the holder's role scope.

`users.py::_set_group_access` enforces an invariant: only a groups-scoped role
may hold group access ("Only users with a group-scoped role can be assigned
group access."). But nothing kept that invariant true when the ROLE changed
underneath the user:

  - `roles.py::update_role` can flip scope_type from "groups" to "all"/"own"
    and never touched employee_group_access.
  - `roles.py::delete_role` sets role_id = NULL on every holder and likewise
    left their group-access rows behind.

Either path leaves rows that the invariant says cannot exist. They are inert
for visibility (scoping.py only consults them for a "groups"-scoped caller),
but they are still returned in UserOut.group_ids, so the Users modal seeds its
checkbox state from them and sends them back on the next save — which the guard
then rejects with a 400, making the user un-editable until the rows are cleared.

These tests pin the cleanup at the point the role changes, which is the only
place that can know the invariant was just broken.
"""
import uuid

import pytest
from sqlalchemy import select

from app.db.models import (
    Company, Employee, EmployeeGroupAccess, Group, Role, RolePermission,
)


# ── Fixtures (same shape as test_role_assignment_escalation.py) ───────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"GA-{tag}", enrollment_token=f"tok-{tag}")
    db.add(c)
    await db.flush()
    return c


async def _role(db, company, name, *, scope_type="groups", is_system=False, perms=()):
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


async def _group(db, company, name):
    g = Group(company_id=company.id, name=f"{name}-{uuid.uuid4().hex[:6]}")
    db.add(g)
    await db.flush()
    return g


async def _grant_group_access(db, emp, group):
    db.add(EmployeeGroupAccess(employee_id=emp.id, group_id=group.id))
    await db.flush()


async def _access_count(db, emp_id) -> int:
    rows = await db.execute(
        select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == emp_id)
    )
    return len(rows.all())


def _headers(emp, company):
    from app.core.security import create_admin_token
    return {"Authorization": f"Bearer {create_admin_token(str(emp.id), emp.role, str(company.id))}"}


async def _all_scope_admin(db, company):
    role = await _role(db, company, f"Owner-{uuid.uuid4().hex[:6]}",
                       scope_type="all", is_system=True)
    return await _employee(db, company, role, "owner")


def _role_body(name, scope_type, perms=(("users", "view"),)):
    return {"name": name, "description": None, "scope_type": scope_type,
            "permissions": [{"module": m, "action": a} for m, a in perms]}


# ── update_role: scope leaving "groups" must clear group access ───────────────

@pytest.mark.asyncio
async def test_changing_role_scope_away_from_groups_clears_group_access(client, db_session):
    company = await _company(db_session)
    admin = await _all_scope_admin(db_session, company)
    rm_role = await _role(db_session, company, "Relation Manager", scope_type="groups")
    holder = await _employee(db_session, company, rm_role, "rm")
    group = await _group(db_session, company, "Team A")
    await _grant_group_access(db_session, holder, group)
    assert await _access_count(db_session, holder.id) == 1

    r = await client.patch(
        f"/v1/roles/{rm_role.id}", json=_role_body("Relation Manager", "all"),
        headers=_headers(admin, company),
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"

    assert await _access_count(db_session, holder.id) == 0, (
        "group access must be cleared when the role stops being groups-scoped — "
        "otherwise the rows violate the invariant _set_group_access enforces"
    )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_changing_role_scope_to_own_also_clears_group_access(client, db_session):
    company = await _company(db_session)
    admin = await _all_scope_admin(db_session, company)
    rm_role = await _role(db_session, company, "Relation Manager", scope_type="groups")
    holder = await _employee(db_session, company, rm_role, "rm")
    group = await _group(db_session, company, "Team B")
    await _grant_group_access(db_session, holder, group)

    r = await client.patch(
        f"/v1/roles/{rm_role.id}", json=_role_body("Relation Manager", "own"),
        headers=_headers(admin, company),
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    assert await _access_count(db_session, holder.id) == 0
    await db_session.rollback()


@pytest.mark.asyncio
async def test_editing_a_groups_role_without_scope_change_keeps_group_access(client, db_session):
    """Regression guard: renaming or re-permissioning a groups-scoped role must
    NOT wipe its holders' group assignments."""
    company = await _company(db_session)
    admin = await _all_scope_admin(db_session, company)
    rm_role = await _role(db_session, company, "Relation Manager", scope_type="groups")
    holder = await _employee(db_session, company, rm_role, "rm")
    group = await _group(db_session, company, "Team C")
    await _grant_group_access(db_session, holder, group)

    r = await client.patch(
        f"/v1/roles/{rm_role.id}",
        json=_role_body("Relation Manager (renamed)", "groups",
                        perms=(("users", "view"), ("devices", "view"))),
        headers=_headers(admin, company),
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    assert await _access_count(db_session, holder.id) == 1, (
        "a scope-preserving edit must leave group access intact"
    )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_scope_change_only_clears_holders_of_that_role(client, db_session):
    """Another role's holders must be untouched."""
    company = await _company(db_session)
    admin = await _all_scope_admin(db_session, company)
    changing = await _role(db_session, company, "Changing RM", scope_type="groups")
    stable   = await _role(db_session, company, "Stable RM",   scope_type="groups")
    a = await _employee(db_session, company, changing, "a")
    b = await _employee(db_session, company, stable,   "b")
    group = await _group(db_session, company, "Team D")
    await _grant_group_access(db_session, a, group)
    await _grant_group_access(db_session, b, group)

    r = await client.patch(
        f"/v1/roles/{changing.id}", json=_role_body("Changing RM", "all"),
        headers=_headers(admin, company),
    )
    assert r.status_code == 200
    assert await _access_count(db_session, a.id) == 0, "the changed role's holder is cleared"
    assert await _access_count(db_session, b.id) == 1, "an unrelated role's holder is untouched"
    await db_session.rollback()


# ── delete_role: holders lose their role, so group access is meaningless ──────

@pytest.mark.asyncio
async def test_deleting_a_groups_role_clears_its_holders_group_access(client, db_session):
    company = await _company(db_session)
    admin_role = await _role(db_session, company, f"Owner-{uuid.uuid4().hex[:6]}",
                             scope_type="all", is_system=True)
    admin = await _employee(db_session, company, admin_role, "owner")
    rm_role = await _role(db_session, company, "Doomed RM", scope_type="groups")
    holder = await _employee(db_session, company, rm_role, "rm")
    group = await _group(db_session, company, "Team E")
    await _grant_group_access(db_session, holder, group)

    r = await client.delete(f"/v1/roles/{rm_role.id}", headers=_headers(admin, company))
    assert r.status_code == 204, f"{r.status_code} {r.text[:200]}"

    assert await _access_count(db_session, holder.id) == 0, (
        "delete_role sets role_id=NULL, so the holder has no role at all — "
        "leaving group access rows behind makes them un-editable"
    )
    await db_session.rollback()


# ── The invariant itself still holds ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_guard_still_rejects_group_access_for_a_non_groups_role(client, db_session):
    """The cleanup must not soften the guard: a request that genuinely asks for
    group access on a non-groups role is still incoherent and must 400."""
    company = await _company(db_session)
    admin = await _all_scope_admin(db_session, company)
    all_role = await _role(db_session, company, "All Scope", scope_type="all")
    target = await _employee(db_session, company, None, "target")
    group = await _group(db_session, company, "Team F")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}",
        json={"role_id": str(all_role.id), "group_ids": [str(group.id)]},
        headers=_headers(admin, company),
    )
    assert r.status_code == 400
    assert "group-scoped role" in r.json()["detail"]
    await db_session.rollback()


@pytest.mark.asyncio
async def test_clearing_group_access_on_a_non_groups_role_is_allowed(client, db_session):
    """What the fixed dashboard sends when a user is moved off a groups-scoped
    role: the new role plus an EMPTY group list. This must succeed and drop the
    now-invalid rows."""
    company = await _company(db_session)
    admin = await _all_scope_admin(db_session, company)
    rm_role  = await _role(db_session, company, "RM", scope_type="groups")
    all_role = await _role(db_session, company, "All Scope", scope_type="all")
    target = await _employee(db_session, company, rm_role, "target")
    group = await _group(db_session, company, "Team G")
    await _grant_group_access(db_session, target, group)

    r = await client.patch(
        f"/v1/users/{target.id}",
        json={"role_id": str(all_role.id), "group_ids": []},
        headers=_headers(admin, company),
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    assert r.json()["group_ids"] == []
    assert await _access_count(db_session, target.id) == 0
    await db_session.rollback()
