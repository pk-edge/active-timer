"""Tests for device archive and soft-delete behaviour."""
import uuid
import pytest

from app.db.base import AsyncSessionLocal
from app.db.models import Company, Device, Employee


@pytest.mark.asyncio
async def test_device_model_has_archive_delete_fields():
    """Device model must expose is_archived and is_deleted with correct defaults."""
    async with AsyncSessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"ArchTest-{tag}", enrollment_token=f"tok-{tag}")
        db.add(company)
        await db.flush()

        device = Device(
            company_id=company.id,
            hostname=f"host-{tag}",
            platform="macos",
        )
        db.add(device)
        await db.flush()

        assert device.is_active   is True
        assert device.is_archived is False
        assert device.is_deleted  is False

        await db.rollback()


# ── helpers ────────────────────────────────────────────────────────────────────

import httpx
from app.main import create_app
from app.api.deps import get_db


def _make_admin_token(emp, company_id):
    from app.core.security import create_admin_token
    return create_admin_token(str(emp.id), emp.role, str(company_id))


async def _make_company_admin(db_session, tag):
    """Create a company + legacy admin employee. Returns (company, emp)."""
    company = Company(name=f"Co-{tag}", enrollment_token=f"enr-{tag}")
    db_session.add(company)
    await db_session.flush()
    emp = Employee(
        company_id=company.id, email=f"admin-{tag}@test.io",
        name="Admin", role="admin", is_active=True, is_approved=True,
        password_hash="x",
    )
    db_session.add(emp)
    await db_session.flush()
    return company, emp


# ── Task 3: enrollment re-registration ────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleted_device_reenrolls_and_becomes_active(db_session):
    """A deleted device re-enrolling is restored to active (same row, unique constraint preserved)."""
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"ReEnroll-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    deleted_device = Device(
        company_id=company.id, hostname=f"host-{tag}",
        platform="macos", is_active=False, is_deleted=True,
    )
    db_session.add(deleted_device)
    await db_session.flush()
    deleted_id = deleted_device.id

    app = create_app()

    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/v1/auth/enroll", json={
            "enrollment_token": f"tok-{tag}",
            "hostname": f"host-{tag}",
            "platform": "macos",
            "agent_version": "1.0.0",
        })
    app.dependency_overrides.clear()

    assert r.status_code == 200
    assert r.json()["device_id"] == str(deleted_id), "Same row reused (unique hostname constraint)"

    await db_session.refresh(deleted_device)
    assert deleted_device.is_deleted  is False, "Device must be un-deleted after re-enroll"
    assert deleted_device.is_active   is True,  "Device must be active after re-enroll"
    assert deleted_device.is_archived is False

    await db_session.rollback()


@pytest.mark.asyncio
async def test_archived_device_reenroll_stays_inactive(db_session):
    """An archived device re-enrolling gets a refreshed token but stays is_active=False."""
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"ArchivedReEnroll-{tag}", enrollment_token=f"tok2-{tag}")
    db_session.add(company)
    await db_session.flush()

    archived_device = Device(
        company_id=company.id, hostname=f"host2-{tag}",
        platform="macos", is_active=False, is_archived=True,
    )
    db_session.add(archived_device)
    await db_session.flush()
    archived_id = archived_device.id

    app = create_app()

    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/v1/auth/enroll", json={
            "enrollment_token": f"tok2-{tag}",
            "hostname": f"host2-{tag}",
            "platform": "macos",
            "agent_version": "1.0.0",
        })
    app.dependency_overrides.clear()

    assert r.status_code == 200
    assert r.json()["device_id"] == str(archived_id), "Archived device should keep its ID"

    await db_session.refresh(archived_device)
    assert archived_device.is_active   is False, "Archived device must remain inactive after re-enroll"
    assert archived_device.is_archived is True

    await db_session.rollback()


# ── Task 4: list_devices with show_archived toggle ─────────────────────────────

@pytest.mark.asyncio
async def test_list_devices_default_excludes_archived_and_deleted(client, db_session):
    """Default GET /devices shows only active devices."""
    tag = uuid.uuid4().hex[:8]
    company, emp = await _make_company_admin(db_session, tag)

    active   = Device(company_id=company.id, hostname=f"active-{tag}",   platform="macos", is_active=True)
    archived = Device(company_id=company.id, hostname=f"archived-{tag}", platform="macos", is_active=False, is_archived=True)
    deleted  = Device(company_id=company.id, hostname=f"deleted-{tag}",  platform="macos", is_active=False, is_deleted=True)
    db_session.add_all([active, archived, deleted])
    await db_session.flush()

    token = _make_admin_token(emp, company.id)
    r = await client.get("/v1/devices", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    hostnames = {d["hostname"] for d in r.json()}
    assert f"active-{tag}"   in hostnames
    assert f"archived-{tag}" not in hostnames, "Archived must not appear in default list"
    assert f"deleted-{tag}"  not in hostnames, "Deleted must never appear"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_list_devices_show_archived_includes_archived_not_deleted(client, db_session):
    """GET /devices?show_archived=true shows active + archived, never deleted."""
    tag = uuid.uuid4().hex[:8]
    company, emp = await _make_company_admin(db_session, tag)

    active   = Device(company_id=company.id, hostname=f"sa-active-{tag}",   platform="macos", is_active=True)
    archived = Device(company_id=company.id, hostname=f"sa-archived-{tag}", platform="macos", is_active=False, is_archived=True)
    deleted  = Device(company_id=company.id, hostname=f"sa-deleted-{tag}",  platform="macos", is_active=False, is_deleted=True)
    db_session.add_all([active, archived, deleted])
    await db_session.flush()

    token = _make_admin_token(emp, company.id)
    r = await client.get("/v1/devices?show_archived=true", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    hostnames = {d["hostname"] for d in r.json()}
    assert f"sa-active-{tag}"   in hostnames
    assert f"sa-archived-{tag}" in hostnames,  "Archived must appear with show_archived=true"
    assert f"sa-deleted-{tag}"  not in hostnames, "Deleted must never appear"

    archived_entry = next(d for d in r.json() if d["hostname"] == f"sa-archived-{tag}")
    assert archived_entry["is_archived"] is True
    assert archived_entry["is_deleted"]  is False

    await db_session.rollback()


# ── Task 5: archive / restore / delete endpoints ──────────────────────────────

async def _setup_company_admin_device(db_session, tag):
    """Create company + admin + one active device. Returns (company, emp, device, token)."""
    company, emp = await _make_company_admin(db_session, tag)
    device = Device(company_id=company.id, hostname=f"crud-host-{tag}", platform="macos", is_active=True)
    db_session.add(device)
    await db_session.flush()
    token = _make_admin_token(emp, company.id)
    return company, emp, device, token


@pytest.mark.asyncio
async def test_archive_device(client, db_session):
    tag = uuid.uuid4().hex[:8]
    _, _, device, token = await _setup_company_admin_device(db_session, tag)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/devices/{device.id}/archive", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["is_archived"] is True
    assert body["is_online"]   is False

    await db_session.refresh(device)
    assert device.is_active   is False
    assert device.is_archived is True
    assert device.is_deleted  is False

    await db_session.rollback()


@pytest.mark.asyncio
async def test_restore_device(client, db_session):
    tag = uuid.uuid4().hex[:8]
    _, _, device, token = await _setup_company_admin_device(db_session, tag)
    device.is_active   = False
    device.is_archived = True
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/devices/{device.id}/restore", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["is_archived"] is False

    await db_session.refresh(device)
    assert device.is_active   is True
    assert device.is_archived is False

    await db_session.rollback()


@pytest.mark.asyncio
async def test_delete_device_returns_204(client, db_session):
    tag = uuid.uuid4().hex[:8]
    _, _, device, token = await _setup_company_admin_device(db_session, tag)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.delete(f"/v1/devices/{device.id}", headers=headers)
    assert r.status_code == 204

    await db_session.refresh(device)
    assert device.is_deleted is True
    assert device.is_active  is False

    await db_session.rollback()


@pytest.mark.asyncio
async def test_deleted_device_returns_404_on_manage_endpoints(client, db_session):
    """A deleted device is inaccessible via all device-management endpoints."""
    tag = uuid.uuid4().hex[:8]
    _, _, device, token = await _setup_company_admin_device(db_session, tag)
    device.is_deleted = True
    device.is_active  = False
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/devices/{device.id}/archive", headers=headers)
    assert r.status_code == 404

    r = await client.patch(f"/v1/devices/{device.id}/assign", headers=headers, json={"employee_id": None})
    assert r.status_code == 404

    await db_session.rollback()


@pytest.mark.asyncio
async def test_cannot_archive_already_archived(client, db_session):
    tag = uuid.uuid4().hex[:8]
    _, _, device, token = await _setup_company_admin_device(db_session, tag)
    device.is_active   = False
    device.is_archived = True
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/devices/{device.id}/archive", headers=headers)
    assert r.status_code == 409

    await db_session.rollback()


@pytest.mark.asyncio
async def test_cannot_restore_active_device(client, db_session):
    tag = uuid.uuid4().hex[:8]
    _, _, device, token = await _setup_company_admin_device(db_session, tag)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch(f"/v1/devices/{device.id}/restore", headers=headers)
    assert r.status_code == 409

    await db_session.rollback()


# ── Task 6: data isolation ─────────────────────────────────────────────────────

from app.core.scoping import visible_device_ids, ALL


class _ScopedUser:
    def __init__(self, employee_id, scope_type):
        self.id = employee_id
        self._scope_type = scope_type


@pytest.mark.asyncio
async def test_own_scope_excludes_archived_and_deleted_devices():
    """visible_device_ids for 'own' scope must exclude archived and deleted devices."""
    async with AsyncSessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"Scope-{tag}", enrollment_token=f"stok-{tag}")
        db.add(company)
        await db.flush()

        emp = Employee(company_id=company.id, email=f"emp-{tag}@x.io", name="Emp")
        db.add(emp)
        await db.flush()

        active   = Device(company_id=company.id, hostname=f"s-active-{tag}",   platform="macos", employee_id=emp.id, is_active=True)
        archived = Device(company_id=company.id, hostname=f"s-archived-{tag}", platform="macos", employee_id=emp.id, is_active=False, is_archived=True)
        deleted  = Device(company_id=company.id, hostname=f"s-deleted-{tag}",  platform="macos", employee_id=emp.id, is_active=False, is_deleted=True)
        db.add_all([active, archived, deleted])
        await db.flush()

        ids = await visible_device_ids(_ScopedUser(emp.id, "own"), db)
        assert ids is not ALL
        id_set = set(ids)
        assert active.id   in id_set,     "Active device must be visible"
        assert archived.id not in id_set, "Archived device must not be visible in own scope"
        assert deleted.id  not in id_set, "Deleted device must not be visible in own scope"

        await db.rollback()


@pytest.mark.asyncio
async def test_deleted_device_returns_404_on_reports(client, db_session):
    """A deleted device returns 404 on device-specific report endpoints."""
    tag = uuid.uuid4().hex[:8]
    company, emp = await _make_company_admin(db_session, tag)

    device = Device(
        company_id=company.id, hostname=f"rep-host-{tag}",
        platform="macos", is_active=False, is_deleted=True,
    )
    db_session.add(device)
    await db_session.flush()

    token = _make_admin_token(emp, company.id)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get(f"/v1/reports/device/{device.id}/summary?days=7", headers=headers)
    assert r.status_code == 404, "Deleted device summary must return 404"

    r = await client.get(f"/v1/reports/device/{device.id}/top-apps?days=7", headers=headers)
    assert r.status_code == 404, "Deleted device top-apps must return 404"

    await db_session.rollback()
