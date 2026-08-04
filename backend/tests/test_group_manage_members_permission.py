"""groups.manage_members must gate add/remove of group members independently
of groups.edit (rename/reparent). See
docs/superpowers/specs/2026-07-08-group-manage-members-permission-design.md"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.security import create_admin_token
from app.db.models import Company, Employee, Group, Role, RolePermission


def _make_token(emp, company_id):
    return create_admin_token(str(emp.id), emp.role, str(company_id))


async def _make_scoped_user(db_session, company, perms):
    """perms: list[(module, action)]. Creates a non-system, all-scope role with
    exactly those permissions and an active employee assigned to it."""
    tag = uuid.uuid4().hex[:6]
    role = Role(company_id=company.id, name=f"Role-{tag}", scope_type="all", is_system=False)
    db_session.add(role)
    await db_session.flush()
    for module, action in perms:
        db_session.add(RolePermission(role_id=role.id, module=module, action=action))
    await db_session.flush()

    emp = Employee(
        company_id=company.id, email=f"user-{tag}@test.io", name=f"User {tag}",
        role_id=role.id, is_active=True, is_approved=True, password_hash="x",
    )
    db_session.add(emp)
    await db_session.flush()
    return emp, _make_token(emp, company.id)


@pytest.mark.asyncio
async def test_manage_members_permission_allows_add_remove_but_not_edit(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"MgmtMem-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    group = Group(company_id=company.id, name=f"Grp-{tag}")
    db_session.add(group)
    await db_session.flush()

    member_emp = Employee(company_id=company.id, email=f"member-{tag}@test.io", name="Member")
    db_session.add(member_emp)
    await db_session.flush()

    _, token = await _make_scoped_user(db_session, company, [("groups", "view"), ("groups", "manage_members")])
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(f"/v1/groups/{group.id}/members", headers=headers,
                          json={"member_type": "employee", "employee_id": str(member_emp.id)})
    assert r.status_code == 201, r.text
    member_id = r.json()["id"]

    r = await client.delete(f"/v1/groups/{group.id}/members/{member_id}", headers=headers)
    assert r.status_code == 204, r.text

    r = await client.patch(f"/v1/groups/{group.id}", headers=headers, json={"name": f"Renamed-{tag}"})
    assert r.status_code == 403, "manage_members alone must not allow renaming the group"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_edit_permission_alone_no_longer_allows_add_member(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"EditOnly-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    group = Group(company_id=company.id, name=f"Grp-{tag}")
    db_session.add(group)
    await db_session.flush()

    member_emp = Employee(company_id=company.id, email=f"member2-{tag}@test.io", name="Member2")
    db_session.add(member_emp)
    await db_session.flush()

    _, token = await _make_scoped_user(db_session, company, [("groups", "view"), ("groups", "edit")])
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/groups/{group.id}", headers=headers, json={"name": f"Renamed2-{tag}"})
    assert r.status_code == 200, r.text

    r = await client.post(f"/v1/groups/{group.id}/members", headers=headers,
                          json={"member_type": "employee", "employee_id": str(member_emp.id)})
    assert r.status_code == 403, "edit alone must no longer allow adding members"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_backfill_migration_grants_manage_members_to_edit_holders(db_session):
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"Backfill-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    role = Role(company_id=company.id, name=f"Legacy-{tag}", scope_type="all", is_system=False)
    db_session.add(role)
    await db_session.flush()
    db_session.add(RolePermission(role_id=role.id, module="groups", action="edit"))
    await db_session.flush()

    migration_sql = Path(__file__).parent.parent.joinpath(
        "app", "db", "migrations", "032_group_manage_members_permission.sql"
    ).read_text()
    await db_session.execute(text(migration_sql))
    await db_session.flush()

    rows = (await db_session.execute(
        text("SELECT action FROM role_permissions WHERE role_id = :rid AND module = 'groups'"),
        {"rid": str(role.id)},
    )).scalars().all()
    assert set(rows) == {"edit", "manage_members"}

    await db_session.rollback()
