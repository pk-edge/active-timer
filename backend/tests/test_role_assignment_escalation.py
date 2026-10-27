"""Anti-escalation for ASSIGNING an existing role to a user.

roles.py already guards CREATING/EDITING a role (_assert_can_grant,
_assert_can_touch_system_role) so a role-manager can't define a role with more
power than they hold. But nothing stopped that same role-manager from just
handing a user (via POST /v1/users, PATCH /v1/users/{id}, or the approve flow)
a role that ALREADY exists with more power — including Super Admin itself. This
covers _assert_can_assign_role, which closes that gap at all three call sites.
"""
import uuid

import pytest

from app.db.models import Company, Employee, Role, RolePermission


async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"Esc-{tag}", enrollment_token=f"tok-{tag}")
    db.add(c)
    await db.flush()
    return c


async def _role(db, company, name, *, scope_type="all", is_system=False, perms=()):
    r = Role(company_id=company.id, name=name, scope_type=scope_type, is_system=is_system)
    db.add(r)
    await db.flush()
    for module, action in perms:
        db.add(RolePermission(role_id=r.id, module=module, action=action))
    await db.flush()
    return r


async def _employee(db, company, role, label, *, is_active=True, is_approved=True):
    tag = uuid.uuid4().hex[:6]
    e = Employee(
        company_id=company.id, email=f"{label}-{tag}@test.io", name=f"{label} {tag}",
        role_id=role.id if role else None, is_active=is_active, is_approved=is_approved,
        password_hash="x",
    )
    db.add(e)
    await db.flush()
    return e


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _headers(emp, company):
    from app.core.security import create_admin_token
    return _auth(create_admin_token(str(emp.id), emp.role, str(company.id)))


@pytest.mark.asyncio
async def test_cannot_patch_a_user_to_super_admin(client, db_session):
    """The original report: a users:edit holder must not be able to grant the
    system Super Admin role through the users endpoint."""
    company = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    # scope_type="groups", not "all": per roles.py's own convention an
    # all-scope caller is already trusted to touch system roles, so the
    # realistic blocked caller is a groups-scoped manager.
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="groups",
        perms=[("users", "view"), ("users", "edit")],
    )
    manager = await _employee(db_session, company, manager_role, "manager")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(sa_role.id)},
        headers=_headers(manager, company),
    )
    assert r.status_code == 403

    await db_session.rollback()


@pytest.mark.asyncio
async def test_cannot_create_a_user_as_super_admin(client, db_session):
    company = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="groups",
        perms=[("users", "view"), ("users", "create")],
    )
    manager = await _employee(db_session, company, manager_role, "manager")
    await db_session.flush()

    r = await client.post(
        "/v1/users",
        json={"name": "New Hire", "email": f"nh-{uuid.uuid4().hex[:6]}@test.io",
              "role_id": str(sa_role.id)},
        headers=_headers(manager, company),
    )
    assert r.status_code == 403

    await db_session.rollback()


@pytest.mark.asyncio
async def test_cannot_approve_a_user_into_super_admin(client, db_session):
    company = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="groups",
        perms=[("users", "view"), ("users", "edit")],
    )
    manager = await _employee(db_session, company, manager_role, "manager")
    pending = await _employee(db_session, company, None, "pending", is_approved=False)
    await db_session.flush()

    r = await client.post(
        f"/v1/users/{pending.id}/approve",
        json={"role_id": str(sa_role.id), "group_ids": []},
        headers=_headers(manager, company),
    )
    assert r.status_code == 403

    await db_session.rollback()


@pytest.mark.asyncio
async def test_cannot_assign_all_scope_role_without_all_scope(client, db_session):
    """Not just Super Admin — ANY all-scope role is out of reach for a
    groups-scoped caller, matching roles.py's own scope_type check."""
    company = await _company(db_session)
    all_scope_role = await _role(db_session, company, "Custom All-Scope Role",
                                 scope_type="all", is_system=False)
    scoped_manager_role = await _role(
        db_session, company, "Scoped Ops", scope_type="groups",
        perms=[("users", "view"), ("users", "edit")],
    )
    manager = await _employee(db_session, company, scoped_manager_role, "manager")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(all_scope_role.id)},
        headers=_headers(manager, company),
    )
    assert r.status_code == 403

    await db_session.rollback()


@pytest.mark.asyncio
async def test_cannot_assign_role_with_permissions_caller_lacks(client, db_session):
    """Even an all-scope role is blocked if it carries a permission the caller
    doesn't hold themselves — mirrors _assert_can_grant's containment check."""
    company = await _company(db_session)
    powerful_role = await _role(
        db_session, company, "Billing Admin", scope_type="all",
        perms=[("users", "view"), ("users", "edit"), ("users", "delete"), ("settings", "edit")],
    )
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="all",
        perms=[("users", "view"), ("users", "edit")],   # lacks users.delete + settings.edit
    )
    manager = await _employee(db_session, company, manager_role, "manager")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(powerful_role.id)},
        headers=_headers(manager, company),
    )
    assert r.status_code == 403
    assert "settings.edit" in r.json()["detail"]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_super_admin_can_still_assign_super_admin(client, db_session):
    """The guard must not lock out the legitimate case: an all-scope system
    user holds every permission, so it can grant any role including its own."""
    company = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(sa_role.id)},
        headers=_headers(admin, company),
    )
    assert r.status_code == 200
    assert r.json()["role_id"] == str(sa_role.id)

    await db_session.rollback()


@pytest.mark.asyncio
async def test_can_still_assign_a_role_within_ones_own_power(client, db_session):
    """Sanity check the guard isn't overly broad: a same-or-lesser role is fine."""
    company = await _company(db_session)
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="all",
        perms=[("users", "view"), ("users", "edit")],
    )
    lesser_role = await _role(
        db_session, company, "Read Only Ops", scope_type="all",
        perms=[("users", "view")],
    )
    manager = await _employee(db_session, company, manager_role, "manager")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(lesser_role.id)},
        headers=_headers(manager, company),
    )
    assert r.status_code == 200

    await db_session.rollback()


@pytest.mark.asyncio
async def test_unassigning_a_role_is_always_allowed(client, db_session):
    """Removing a role only ever reduces privilege — never blocked."""
    company = await _company(db_session)
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="all",
        perms=[("users", "view"), ("users", "edit")],
    )
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    manager = await _employee(db_session, company, manager_role, "manager")
    target = await _employee(db_session, company, sa_role, "target")
    await db_session.flush()

    # An empty role_id is falsy, so _resolve_role treats it as "no role" —
    # this is the unassign path a scoped manager must still be allowed to take
    # even though they could never have GRANTED the role being removed.
    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": ""},
        headers=_headers(manager, company),
    )
    assert r.status_code == 200
    assert r.json()["role_id"] is None

    await db_session.rollback()


# ── Retired-permission drift (the "screenshots.view" report) ──────────────────
# A Super Admin could not assign the Super Admin role: the role carried a
# `screenshots.view` row left behind when bb0b17c removed the `screenshots`
# module from permissions.py without a cleanup migration (42 such rows in
# production). The two sides of the containment check read from different
# places — the caller's `held` set is built in CODE from all_permissions(),
# the target role's from the DATABASE — so the orphan row appeared only on the
# role's side and was reported as a permission the Super Admin "does not hold".
#
# deps.py already tolerates the opposite drift (code ahead of DB, when a new
# module is added before rows exist). These cover DB ahead of code.

RETIRED = ("screenshots", "view")   # removed from the catalog in bb0b17c


@pytest.mark.asyncio
async def test_super_admin_can_assign_super_admin_with_a_retired_permission(client, db_session):
    """The reported failure, end to end."""
    from app.core.permissions import is_valid
    assert not is_valid(*RETIRED), "this test is meaningless if the pair is still in the catalog"

    company = await _company(db_session)
    sa_role = await _role(
        db_session, company, "Super Admin", scope_type="all", is_system=True,
        perms=[("users", "view"), ("users", "edit"), RETIRED],
    )
    super_admin = await _employee(db_session, company, sa_role, "sa")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(sa_role.id)},
        headers=_headers(super_admin, company),
    )
    assert r.status_code == 200, (
        f"a Super Admin must be able to assign Super Admin; got {r.status_code} {r.text[:200]}"
    )
    assert r.json()["role_name"] == "Super Admin"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_retired_permission_does_not_block_a_normal_role_assignment(client, db_session):
    """Same drift on an ordinary (non-system) role and an ordinary caller: the
    retired pair must be ignored on BOTH sides, not special-cased for Super Admin."""
    company = await _company(db_session)
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="groups",
        perms=[("users", "view"), ("users", "edit")],
    )
    manager = await _employee(db_session, company, manager_role, "manager")
    # A role the manager is otherwise entitled to hand out, polluted with the
    # retired pair.
    viewer_role = await _role(
        db_session, company, "Viewer", scope_type="groups",
        perms=[("users", "view"), RETIRED],
    )
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(viewer_role.id)},
        headers=_headers(manager, company),
    )
    assert r.status_code == 200, f"got {r.status_code} {r.text[:200]}"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_a_real_missing_permission_is_still_refused(client, db_session):
    """Regression guard on the guard: ignoring RETIRED pairs must not soften the
    check for permissions that genuinely exist and the caller genuinely lacks."""
    company = await _company(db_session)
    manager_role = await _role(
        db_session, company, "People Ops", scope_type="groups",
        perms=[("users", "view"), ("users", "edit")],
    )
    manager = await _employee(db_session, company, manager_role, "manager")
    powerful = await _role(
        db_session, company, "Auditor", scope_type="groups",
        perms=[("users", "view"), ("audit", "view"), RETIRED],
    )
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.patch(
        f"/v1/users/{target.id}", json={"role_id": str(powerful.id)},
        headers=_headers(manager, company),
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "audit.view" in detail, f"the real gap must still be named: {detail}"
    assert "screenshots" not in detail, f"a retired pair must never be reported: {detail}"

    await db_session.rollback()
