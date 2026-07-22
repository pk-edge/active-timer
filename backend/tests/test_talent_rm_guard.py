"""Talent users must never be grantable group-view (Relation Manager) access,
but changing a Talent user's role must never be blocked by this guard."""
import uuid
import pytest

from app.db.models import Company, Employee, Group, Role
from app.core.provisioning import TALENT_ROLE_NAME


def _make_admin_token(emp, company_id):
    from app.core.security import create_admin_token
    return create_admin_token(str(emp.id), emp.role, str(company_id))


async def _setup(db_session, tag):
    """Company + legacy admin actor + a Talent employee + a Viewer employee + a group."""
    company = Company(name=f"TalentGuard-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin = Employee(
        company_id=company.id, email=f"admin-{tag}@test.io", name="Admin",
        role="admin", is_active=True, is_approved=True, password_hash="x",
    )
    db_session.add(admin)
    await db_session.flush()

    talent_role = Role(company_id=company.id, name=TALENT_ROLE_NAME, scope_type="own", is_system=True)
    viewer_role = Role(company_id=company.id, name="Viewer", scope_type="groups", is_system=False)
    db_session.add_all([talent_role, viewer_role])
    await db_session.flush()

    talent = Employee(
        company_id=company.id, email=f"talent-{tag}@test.io", name="Talent Person",
        role="employee", role_id=talent_role.id, is_active=True, is_approved=True,
    )
    viewer = Employee(
        company_id=company.id, email=f"viewer-{tag}@test.io", name="Viewer Person",
        role="employee", role_id=viewer_role.id, is_active=True, is_approved=True, password_hash="x",
    )
    db_session.add_all([talent, viewer])
    await db_session.flush()

    group = Group(company_id=company.id, name=f"Group-{tag}")
    db_session.add(group)
    await db_session.flush()

    token = _make_admin_token(admin, company.id)
    return admin, talent, viewer, viewer_role, group, token


@pytest.mark.asyncio
async def test_talent_user_cannot_be_granted_group_access(client, db_session):
    tag = uuid.uuid4().hex[:8]
    _, talent, _, _, group, token = await _setup(db_session, tag)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.put(f"/v1/users/{talent.id}/groups", headers=headers, json=[str(group.id)])
    assert r.status_code == 400
    assert "Relation Manager" in r.json()["detail"]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_groups_scoped_user_can_be_granted_group_access(client, db_session):
    tag = uuid.uuid4().hex[:8]
    _, _, viewer, _, group, token = await _setup(db_session, tag)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.put(f"/v1/users/{viewer.id}/groups", headers=headers, json=[str(group.id)])
    assert r.status_code == 200
    assert r.json()["group_ids"] == [str(group.id)]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_changing_talent_role_and_assigning_groups_in_one_patch_succeeds(client, db_session):
    """The guard must never block a role change — even one that also assigns
    groups in the same request, as long as the NEW role is groups-scoped."""
    tag = uuid.uuid4().hex[:8]
    _, talent, _, viewer_role, group, token = await _setup(db_session, tag)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/users/{talent.id}", headers=headers, json={
        "role_id": str(viewer_role.id), "group_ids": [str(group.id)],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["role_name"] == "Viewer"
    assert body["group_ids"] == [str(group.id)]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_patching_talent_role_field_alone_still_works(client, db_session):
    """Editing a Talent user's role (no group_ids in the request) is never blocked."""
    tag = uuid.uuid4().hex[:8]
    _, talent, _, viewer_role, _, token = await _setup(db_session, tag)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/users/{talent.id}", headers=headers, json={"role_id": str(viewer_role.id)})
    assert r.status_code == 200
    assert r.json()["role_name"] == "Viewer"

    await db_session.rollback()
