"""GET /v1/notifications must surface offline talent devices to Super Admins and
in-scope Relation Managers only, honoring offline_alert_enabled/offline_alert_mins."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Company, Device, Employee, EmployeeGroupAccess, Group, GroupMember, Role
from app.core.provisioning import TALENT_ROLE_NAME


def _make_admin_token(emp, company_id):
    from app.core.security import create_admin_token
    return create_admin_token(str(emp.id), emp.role, str(company_id))


async def _setup_company_with_offline_talent(db_session, tag, offline_settings=None):
    company = Company(
        name=f"Offline-{tag}", enrollment_token=f"tok-{tag}",
        settings=offline_settings if offline_settings is not None
                 else {"offline_alert_enabled": True, "offline_alert_mins": 30},
    )
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

    group = Group(company_id=company.id, name=f"Grp-{tag}")
    other_group = Group(company_id=company.id, name=f"OtherGrp-{tag}")
    db_session.add_all([group, other_group])
    await db_session.flush()

    talent = Employee(
        company_id=company.id, email=f"talent-{tag}@test.io", name="Offline Talent",
        role="employee", role_id=talent_role.id, is_active=True, is_approved=True,
    )
    db_session.add(talent)
    await db_session.flush()

    stale_seen = datetime.now(timezone.utc) - timedelta(minutes=90)
    device = Device(
        company_id=company.id, hostname=f"host-{tag}", platform="macos",
        employee_id=talent.id, is_active=True, last_seen=stale_seen,
    )
    db_session.add(device)
    await db_session.flush()

    db_session.add(GroupMember(group_id=group.id, member_type="employee", employee_id=talent.id))
    await db_session.flush()

    rm_in_scope = Employee(
        company_id=company.id, email=f"rm-in-{tag}@test.io", name="RM In Scope",
        role="employee", role_id=viewer_role.id, is_active=True, is_approved=True, password_hash="x",
    )
    rm_out_of_scope = Employee(
        company_id=company.id, email=f"rm-out-{tag}@test.io", name="RM Out Of Scope",
        role="employee", role_id=viewer_role.id, is_active=True, is_approved=True, password_hash="x",
    )
    db_session.add_all([rm_in_scope, rm_out_of_scope])
    await db_session.flush()
    db_session.add(EmployeeGroupAccess(employee_id=rm_in_scope.id, group_id=group.id))
    db_session.add(EmployeeGroupAccess(employee_id=rm_out_of_scope.id, group_id=other_group.id))
    await db_session.flush()

    return company, admin, talent, device, rm_in_scope, rm_out_of_scope, group


@pytest.mark.asyncio
async def test_super_admin_sees_offline_talent_device(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company, admin, talent, device, _, _, group = await _setup_company_with_offline_talent(db_session, tag)
    token = _make_admin_token(admin, company.id)

    r = await client.get("/v1/notifications", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    alerts = [n for n in r.json() if n["type"] == "talent_offline"]
    assert len(alerts) == 1
    assert talent.name in alerts[0]["title"]
    assert device.hostname in alerts[0]["body"]
    assert group.name in alerts[0]["body"]
    assert alerts[0]["metadata"]["device_id"] == str(device.id)
    assert str(device.id) not in alerts[0]["title"]
    assert str(device.id) not in alerts[0]["body"]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_rm_in_scope_sees_it_rm_out_of_scope_does_not(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company, _, talent, device, rm_in, rm_out, _ = await _setup_company_with_offline_talent(db_session, tag)

    token_in = _make_admin_token(rm_in, company.id)
    r_in = await client.get("/v1/notifications", headers={"Authorization": f"Bearer {token_in}"})
    assert r_in.status_code == 200
    assert any(n["type"] == "talent_offline" for n in r_in.json())

    token_out = _make_admin_token(rm_out, company.id)
    r_out = await client.get("/v1/notifications", headers={"Authorization": f"Bearer {token_out}"})
    assert r_out.status_code == 200
    assert not any(n["type"] == "talent_offline" for n in r_out.json())

    await db_session.rollback()


@pytest.mark.asyncio
async def test_disabled_setting_suppresses_alert(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company, admin, *_ = await _setup_company_with_offline_talent(
        db_session, tag, offline_settings={"offline_alert_enabled": False, "offline_alert_mins": 30},
    )
    token = _make_admin_token(admin, company.id)

    r = await client.get("/v1/notifications", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert not any(n["type"] == "talent_offline" for n in r.json())

    await db_session.rollback()


@pytest.mark.asyncio
async def test_recently_seen_device_does_not_alert(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company, admin, talent, device, *_ = await _setup_company_with_offline_talent(db_session, tag)
    device.last_seen = datetime.now(timezone.utc) - timedelta(minutes=2)
    await db_session.flush()
    token = _make_admin_token(admin, company.id)

    r = await client.get("/v1/notifications", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert not any(n["type"] == "talent_offline" for n in r.json())

    await db_session.rollback()
