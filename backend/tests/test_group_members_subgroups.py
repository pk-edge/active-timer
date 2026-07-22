"""GET /v1/groups/{id}/members?include_subgroups=true must include members
inherited from subgroups, tagged with their source group and direct/inherited."""
import uuid
import pytest

from app.db.models import Company, Employee, Group, GroupMember


def _make_admin_token(emp, company_id):
    from app.core.security import create_admin_token
    return create_admin_token(str(emp.id), emp.role, str(company_id))


@pytest.mark.asyncio
async def test_include_subgroups_returns_direct_and_inherited_members(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"SubMem-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin = Employee(
        company_id=company.id, email=f"admin-{tag}@test.io", name="Admin",
        role="admin", is_active=True, is_approved=True, password_hash="x",
    )
    db_session.add(admin)
    await db_session.flush()

    parent = Group(company_id=company.id, name=f"Parent-{tag}")
    db_session.add(parent)
    await db_session.flush()
    child = Group(company_id=company.id, name=f"Child-{tag}", parent_id=parent.id)
    db_session.add(child)
    await db_session.flush()

    direct_emp = Employee(company_id=company.id, email=f"direct-{tag}@test.io", name="Direct Person")
    inherited_emp = Employee(company_id=company.id, email=f"inherited-{tag}@test.io", name="Inherited Person")
    db_session.add_all([direct_emp, inherited_emp])
    await db_session.flush()

    db_session.add_all([
        GroupMember(group_id=parent.id, member_type="employee", employee_id=direct_emp.id),
        GroupMember(group_id=child.id, member_type="employee", employee_id=inherited_emp.id),
    ])
    await db_session.flush()

    token = _make_admin_token(admin, company.id)
    headers = {"Authorization": f"Bearer {token}"}

    r_direct = await client.get(f"/v1/groups/{parent.id}/members", headers=headers)
    assert r_direct.status_code == 200
    direct_only = r_direct.json()
    assert {m["employee_id"] for m in direct_only} == {str(direct_emp.id)}
    assert direct_only[0]["direct"] is None  # untouched when include_subgroups is not requested

    r_all = await client.get(f"/v1/groups/{parent.id}/members?include_subgroups=true", headers=headers)
    assert r_all.status_code == 200
    all_members = r_all.json()
    by_emp = {m["employee_id"]: m for m in all_members}
    assert set(by_emp) == {str(direct_emp.id), str(inherited_emp.id)}
    assert by_emp[str(direct_emp.id)]["direct"] is True
    assert by_emp[str(direct_emp.id)]["group_name"] == f"Parent-{tag}"
    assert by_emp[str(inherited_emp.id)]["direct"] is False
    assert by_emp[str(inherited_emp.id)]["group_name"] == f"Child-{tag}"
    assert by_emp[str(inherited_emp.id)]["path"] == f"Parent-{tag} / Child-{tag}"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_member_count_reflects_direct_members_only_even_with_subgroups(client, db_session):
    """GET /v1/groups/{id} member_count must stay the DIRECT count regardless of
    include_subgroups — it's a summary badge, not a total."""
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"CountFix-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin = Employee(
        company_id=company.id, email=f"admin-{tag}@test.io", name="Admin",
        role="admin", is_active=True, is_approved=True, password_hash="x",
    )
    db_session.add(admin)
    await db_session.flush()

    parent = Group(company_id=company.id, name=f"Parent2-{tag}")
    db_session.add(parent)
    await db_session.flush()
    child = Group(company_id=company.id, name=f"Child2-{tag}", parent_id=parent.id)
    db_session.add(child)
    await db_session.flush()

    only_child_member = Employee(company_id=company.id, email=f"childmem-{tag}@test.io", name="Child Member")
    db_session.add(only_child_member)
    await db_session.flush()
    db_session.add(GroupMember(group_id=child.id, member_type="employee", employee_id=only_child_member.id))
    await db_session.flush()

    token = _make_admin_token(admin, company.id)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get(f"/v1/groups/{parent.id}?include_subgroups=true", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["member_count"] == 0, "Parent has zero DIRECT members even though include_subgroups=true returns 1"
    assert len(body["members"]) == 1

    await db_session.rollback()
