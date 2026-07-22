"""GET /v1/users must classify each employee as user_kind = 'talent' | 'team'."""
import uuid
import pytest

from app.db.models import Company, Employee, Role
from app.core.provisioning import TALENT_ROLE_NAME


def _make_admin_token(emp, company_id):
    from app.core.security import create_admin_token
    return create_admin_token(str(emp.id), emp.role, str(company_id))


@pytest.mark.asyncio
async def test_list_users_reports_user_kind(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"Kind-{tag}", enrollment_token=f"tok-{tag}")
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
    monitored = Employee(
        company_id=company.id, email=f"mon-{tag}@test.io", name="Monitored Only",
        role="employee", role_id=None, is_active=True, is_approved=True,
    )
    db_session.add_all([talent, viewer, monitored])
    await db_session.flush()

    token = _make_admin_token(admin, company.id)
    r = await client.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    by_email = {u["email"]: u for u in r.json()}

    assert by_email[f"talent-{tag}@test.io"]["user_kind"] == "talent"
    assert by_email[f"viewer-{tag}@test.io"]["user_kind"] == "team"
    assert by_email[f"mon-{tag}@test.io"]["user_kind"] == "team"

    await db_session.rollback()
