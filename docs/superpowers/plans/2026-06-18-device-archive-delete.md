# Device Archive & Soft Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add archive (reversible suspension) and soft-delete (permanent removal from UI) to devices, ensuring archived/deleted devices cannot ingest data and do not appear in active reporting.

**Architecture:** Two new boolean columns (`is_archived`, `is_deleted`) added to the `devices` table mirror the existing `Employee.is_deleted` pattern. The existing `is_active` column already gates all ingest auth, so setting it to `FALSE` on archive/delete is sufficient to block data flow with zero changes to the hot path. A new `?show_archived=true` query param (admin-only) surfaces archived devices in the device list.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL, pytest-asyncio, httpx

## Global Constraints

- Migration must be purely additive (`ADD COLUMN … DEFAULT FALSE`) — zero rows touched, zero data loss.
- `is_active = TRUE` only when `is_archived = FALSE AND is_deleted = FALSE`. Any write that sets `is_archived` or `is_deleted` to `TRUE` must also set `is_active = FALSE`.
- `is_deleted = TRUE` devices never appear in any list response, any report, or any device-specific endpoint — they are logically gone from the UI.
- Tests run inside the backend Docker container against live Postgres. Command: `docker compose run --rm --no-deps backend python -m pytest tests/<file>.py -v`
- Never use `git commit` without explicit user instruction.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `backend/app/db/migrations/017_device_archive_delete.sql` | Create | Additive columns + super-admin permission backfill |
| `backend/app/db/models.py` | Modify | Add `is_archived`, `is_deleted` fields to `Device` |
| `backend/app/schemas/device.py` | Modify | Add `is_archived`, `is_deleted` to `DeviceOut` |
| `backend/app/api/v1/endpoints/auth.py` | Modify | Enrollment re-registration for deleted/archived devices |
| `backend/app/api/v1/endpoints/devices.py` | Modify | `list_devices` toggle; `_get_device_for_admin` guard; 3 new endpoints |
| `backend/app/core/scoping.py` | Modify | Filter archived/deleted from "own" scope |
| `backend/app/api/v1/endpoints/reports.py` | Modify | Guard `_get_visible_device` against deleted devices |
| `backend/tests/test_device_archive_delete.py` | Create | Integration tests for all new behaviour |

---

## Task 1: Migration 017 — Additive columns + permission seed

**Files:**
- Modify: `backend/app/core/permissions.py:17`
- Create: `backend/app/db/migrations/017_device_archive_delete.sql`

**Interfaces:**
- Produces: `devices.is_archived BOOLEAN NOT NULL DEFAULT FALSE`, `devices.is_deleted BOOLEAN NOT NULL DEFAULT FALSE`; `("devices","archive")` and `("devices","delete")` in `MODULES`; `role_permissions` rows backfilled for non-system roles.

**Why permissions.py first:** `deps.py` grants Super Admins `all_permissions()` dynamically at login time — no DB rows needed for system roles. Adding the actions to `permissions.py` is what makes `require("devices","archive")` and `require("devices","delete")` work immediately for Super Admins. The SQL backfill covers non-system roles that explicitly hold `devices` permissions.

- [ ] **Step 1: Add archive and delete actions to permissions.py**

In `backend/app/core/permissions.py`, update the `devices` module entry:
```python
    {"key": "devices", "label": "Devices", "actions": ["view", "assign", "archive", "delete"]},
```

- [ ] **Step 3: Create the migration file**

```sql
-- backend/app/db/migrations/017_device_archive_delete.sql
-- Additive: no rows touched, no data loss.

ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS is_deleted  BOOLEAN NOT NULL DEFAULT FALSE;

-- Grant archive and delete permissions to all existing Super Admin roles.
-- ON CONFLICT DO NOTHING is idempotent — safe to re-run.
INSERT INTO role_permissions (role_id, module, action)
SELECT r.id, perms.module, perms.action
FROM roles r
CROSS JOIN (VALUES
    ('devices', 'archive'),
    ('devices', 'delete')
) AS perms(module, action)
WHERE r.name = 'Super Admin'
  AND r.is_system = TRUE
  AND r.is_deleted = FALSE
ON CONFLICT (role_id, module, action) DO NOTHING;
```

- [ ] **Step 4: Verify the migration applies cleanly**

Run inside the backend container (the migration runner picks up any new file in `migrations/` on startup):
```bash
docker compose run --rm --no-deps backend python -c "
import asyncio
from app.db.base import init_db
asyncio.run(init_db())
print('Migration OK')
"
```
Expected output: `Migration OK` with no errors. Confirm with:
```bash
docker compose run --rm --no-deps backend python -c "
import asyncio
from app.db.base import AsyncSessionLocal
from sqlalchemy import text
async def check():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text(\"SELECT column_name FROM information_schema.columns WHERE table_name='devices' AND column_name IN ('is_archived','is_deleted')\"))
        print([row[0] for row in r.all()])
asyncio.run(check())
"
```
Expected: `['is_archived', 'is_deleted']` (order may vary).

---

## Task 2: Model & Schema updates

**Files:**
- Modify: `backend/app/db/models.py:44-62`
- Modify: `backend/app/schemas/device.py:6-16`
- Create: `backend/tests/test_device_archive_delete.py` (scaffold only — full tests added in later tasks)

**Interfaces:**
- Produces: `Device.is_archived: Mapped[bool]`, `Device.is_deleted: Mapped[bool]`; `DeviceOut.is_archived: bool`, `DeviceOut.is_deleted: bool`

- [ ] **Step 1: Write the failing test (model fields)**

Create `backend/tests/test_device_archive_delete.py`:
```python
"""Tests for device archive and soft-delete behaviour."""
import uuid
import pytest

from app.db.base import AsyncSessionLocal
from app.db.models import Company, Device


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_device_model_has_archive_delete_fields -v
```
Expected: `FAILED` with `AttributeError: 'Device' object has no attribute 'is_archived'`

- [ ] **Step 3: Add fields to the Device model**

In `backend/app/db/models.py`, inside the `Device` class after `is_active`:
```python
    is_active:     Mapped[bool]             = mapped_column(Boolean, default=True)
    is_archived:   Mapped[bool]             = mapped_column(Boolean, default=False)
    is_deleted:    Mapped[bool]             = mapped_column(Boolean, default=False)
```

- [ ] **Step 4: Add fields to the DeviceOut schema**

In `backend/app/schemas/device.py`, update `DeviceOut`:
```python
class DeviceOut(BaseModel):
    id:             str
    hostname:       str
    platform:       str
    employee_id:    Optional[str] = None
    employee_name:  Optional[str]
    last_seen:      Optional[datetime]
    last_seen_unix: Optional[int]
    is_online:      bool
    is_archived:    bool = False
    is_deleted:     bool = False

    model_config = {"from_attributes": True}
```

- [ ] **Step 5: Update both DeviceOut construction sites in devices.py to pass the new fields**

In `backend/app/api/v1/endpoints/devices.py`, the `list_devices` loop (line ~63):
```python
        DeviceOut(
            id=str(d.id),
            hostname=d.hostname,
            platform=d.platform,
            employee_id=str(d.employee_id) if d.employee_id else None,
            employee_name=d.employee.name if d.employee else None,
            last_seen=d.last_seen,
            last_seen_unix=int(d.last_seen.timestamp()) if d.last_seen else None,
            is_online=bool(d.last_seen and d.last_seen > cutoff),
            is_archived=d.is_archived,
            is_deleted=d.is_deleted,
        )
```

And the `assign_device` return (line ~114):
```python
    return DeviceOut(
        id=str(device.id),
        hostname=device.hostname,
        platform=device.platform,
        employee_id=str(employee.id) if employee else None,
        employee_name=employee.name if employee else None,
        last_seen=device.last_seen,
        last_seen_unix=int(device.last_seen.timestamp()) if device.last_seen else None,
        is_online=bool(device.last_seen and device.last_seen > cutoff),
        is_archived=device.is_archived,
        is_deleted=device.is_deleted,
    )
```

- [ ] **Step 6: Run test to verify it passes**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_device_model_has_archive_delete_fields -v
```
Expected: `PASSED`

---

## Task 3: Enrollment re-registration for deleted/archived devices

**Files:**
- Modify: `backend/app/api/v1/endpoints/auth.py:31-48`
- Modify: `backend/tests/test_device_archive_delete.py` (add new tests)

**Interfaces:**
- Consumes: `Device.is_archived`, `Device.is_deleted` from Task 2
- Produces: `POST /v1/auth/enroll` behaviour:
  - Deleted device by same hostname → new Device row created (different ID)
  - Archived device → token refreshed, `is_active` stays `FALSE`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_device_archive_delete.py`:
```python
import httpx
from app.main import create_app
from app.api.deps import get_db


@pytest.mark.asyncio
async def test_deleted_device_reenrolls_as_new(db_session):
    """A deleted device's agent re-enrolling gets a brand-new Device row."""
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
    assert r.json()["device_id"] != str(deleted_id), "Must create a new device, not reuse deleted"

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

    # Same device ID returned (not a new row) but still inactive
    assert r.status_code == 200
    assert r.json()["device_id"] == str(archived_id), "Archived device should keep its ID"

    await db_session.refresh(archived_device)
    assert archived_device.is_active   is False, "Archived device must remain inactive after re-enroll"
    assert archived_device.is_archived is True

    await db_session.rollback()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_deleted_device_reenrolls_as_new tests/test_device_archive_delete.py::test_archived_device_reenroll_stays_inactive -v
```
Expected: Both `FAILED` — `test_deleted_device_reenrolls_as_new` will fail because the current code upserts onto the deleted record; `test_archived_device_reenroll_stays_inactive` will fail because the current code sets `is_active=True` unconditionally.

- [ ] **Step 3: Update the enroll endpoint**

In `backend/app/api/v1/endpoints/auth.py`, replace the upsert block (lines 31-48):
```python
    # Upsert by (company_id, hostname) — skip deleted devices so they get a fresh row.
    result = await db.execute(
        select(Device).where(
            Device.company_id == company.id,
            Device.hostname == body.hostname,
            Device.is_deleted == False,  # noqa: E712
        )
    )
    device = result.scalar_one_or_none()

    if not device:
        device = Device(
            company_id=company.id,
            hostname=body.hostname,
            platform=body.platform,
            agent_version=body.agent_version,
        )
        db.add(device)
        await db.flush()
    else:
        device.platform      = body.platform
        device.agent_version = body.agent_version
        # Archived devices: refresh the token so the agent has a valid credential,
        # but do NOT reactivate — admin must explicitly restore from archive.
        if not device.is_archived:
            device.is_active = True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_deleted_device_reenrolls_as_new tests/test_device_archive_delete.py::test_archived_device_reenroll_stays_inactive -v
```
Expected: Both `PASSED`

---

## Task 4: list_devices with show_archived toggle

**Files:**
- Modify: `backend/app/api/v1/endpoints/devices.py:43-74`
- Modify: `backend/tests/test_device_archive_delete.py` (add new tests)

**Interfaces:**
- Consumes: `Device.is_archived`, `Device.is_deleted`, `DeviceOut.is_archived`, `DeviceOut.is_deleted` from Task 2
- Produces: `GET /v1/devices` — default returns only `is_active=TRUE, is_deleted=FALSE`; `?show_archived=true` also returns `is_archived=TRUE` devices; deleted devices never appear in any response.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_device_archive_delete.py`:
```python
@pytest.mark.asyncio
async def test_list_devices_default_excludes_archived_and_deleted(client, db_session):
    """Default GET /devices shows only active devices."""
    from app.core.security import hash_password
    from app.db.admin import ensure_admin

    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"ListTest-{tag}", enrollment_token=f"ltok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin_emp = await ensure_admin(
        db_session, company.id,
        email=f"admin-{tag}@test.io", name="Admin", password="Pass123!"
    )
    await db_session.flush()

    active   = Device(company_id=company.id, hostname=f"active-{tag}",   platform="macos", is_active=True)
    archived = Device(company_id=company.id, hostname=f"archived-{tag}", platform="macos", is_active=False, is_archived=True)
    deleted  = Device(company_id=company.id, hostname=f"deleted-{tag}",  platform="macos", is_active=False, is_deleted=True)
    db_session.add_all([active, archived, deleted])
    await db_session.flush()

    from app.core.security import create_admin_token
    token = create_admin_token(str(admin_emp.id), admin_emp.role, str(company.id))

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
    from app.db.admin import ensure_admin
    from app.core.security import create_admin_token

    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"ShowArch-{tag}", enrollment_token=f"satk-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin_emp = await ensure_admin(
        db_session, company.id,
        email=f"sa-admin-{tag}@test.io", name="Admin", password="Pass123!"
    )
    await db_session.flush()

    active   = Device(company_id=company.id, hostname=f"sa-active-{tag}",   platform="macos", is_active=True)
    archived = Device(company_id=company.id, hostname=f"sa-archived-{tag}", platform="macos", is_active=False, is_archived=True)
    deleted  = Device(company_id=company.id, hostname=f"sa-deleted-{tag}",  platform="macos", is_active=False, is_deleted=True)
    db_session.add_all([active, archived, deleted])
    await db_session.flush()

    token = create_admin_token(str(admin_emp.id), admin_emp.role, str(company.id))

    r = await client.get("/v1/devices?show_archived=true", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    hostnames = {d["hostname"] for d in r.json()}
    assert f"sa-active-{tag}"   in hostnames
    assert f"sa-archived-{tag}" in hostnames,  "Archived must appear with show_archived=true"
    assert f"sa-deleted-{tag}"  not in hostnames, "Deleted must never appear"

    # Verify is_archived flag in response payload
    archived_entry = next(d for d in r.json() if d["hostname"] == f"sa-archived-{tag}")
    assert archived_entry["is_archived"] is True
    assert archived_entry["is_deleted"]  is False

    await db_session.rollback()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_list_devices_default_excludes_archived_and_deleted tests/test_device_archive_delete.py::test_list_devices_show_archived_includes_archived_not_deleted -v
```
Expected: Both `FAILED` — first test passes incidentally (archived already excluded via `is_active=True`), but second test fails because `show_archived` param doesn't exist yet.

- [ ] **Step 3: Update list_devices**

Replace the `list_devices` function in `backend/app/api/v1/endpoints/devices.py`:
```python
@router.get("", response_model=List[DeviceOut])
async def list_devices(
    show_archived: bool = Query(default=False),
    admin: Employee = Depends(require("devices", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await visible_device_ids(admin, db)
    if ids is not ALL and not ids:
        return []

    if show_archived:
        # Active + archived, but never deleted
        q = (
            select(Device)
            .options(joinedload(Device.employee))
            .where(
                Device.company_id == admin.company_id,
                Device.is_deleted == False,  # noqa: E712
            )
            .order_by(Device.is_active.desc(), Device.last_seen.desc().nullslast())
        )
    else:
        q = (
            select(Device)
            .options(joinedload(Device.employee))
            .where(
                Device.company_id == admin.company_id,
                Device.is_active == True,   # noqa: E712
                Device.is_deleted == False,  # noqa: E712
            )
            .order_by(Device.last_seen.desc().nullslast())
        )

    if ids is not ALL:
        q = q.where(Device.id.in_(ids))

    result  = await db.execute(q)
    devices = result.scalars().all()
    cutoff  = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    return [
        DeviceOut(
            id=str(d.id),
            hostname=d.hostname,
            platform=d.platform,
            employee_id=str(d.employee_id) if d.employee_id else None,
            employee_name=d.employee.name if d.employee else None,
            last_seen=d.last_seen,
            last_seen_unix=int(d.last_seen.timestamp()) if d.last_seen else None,
            is_online=bool(d.last_seen and d.last_seen > cutoff),
            is_archived=d.is_archived,
            is_deleted=d.is_deleted,
        )
        for d in devices
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_list_devices_default_excludes_archived_and_deleted tests/test_device_archive_delete.py::test_list_devices_show_archived_includes_archived_not_deleted -v
```
Expected: Both `PASSED`

---

## Task 5: _get_device_for_admin guard + archive / restore / delete endpoints

**Files:**
- Modify: `backend/app/api/v1/endpoints/devices.py` — `_get_device_for_admin`, 3 new endpoints
- Modify: `backend/tests/test_device_archive_delete.py` (add new tests)

**Interfaces:**
- Consumes: `Device.is_archived`, `Device.is_deleted` from Task 2; `require()` from `app.api.deps`; `audit.record()` from `app.core.audit`
- Produces:
  - `_get_device_for_admin` now filters `is_deleted=FALSE` — deleted devices return 404
  - `PATCH /v1/devices/{id}/archive` → sets `is_active=False, is_archived=True`; requires `("devices","archive")`
  - `PATCH /v1/devices/{id}/restore` → sets `is_active=True, is_archived=False`; requires `("devices","archive")`
  - `DELETE /v1/devices/{id}` → 204; sets `is_deleted=True, is_active=False`; requires `("devices","delete")`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_device_archive_delete.py`:
```python
async def _setup_company_admin_device(db_session, tag):
    """Helper: creates a company, super-admin employee, and one active device.
    Returns (company, admin_emp, device, admin_token)."""
    from app.db.admin import ensure_admin
    from app.core.security import create_admin_token

    company = Company(name=f"CRUD-{tag}", enrollment_token=f"ctok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin_emp = await ensure_admin(
        db_session, company.id,
        email=f"crud-admin-{tag}@test.io", name="Admin", password="Pass123!"
    )
    await db_session.flush()

    device = Device(company_id=company.id, hostname=f"crud-host-{tag}", platform="macos", is_active=True)
    db_session.add(device)
    await db_session.flush()

    token = create_admin_token(str(admin_emp.id), admin_emp.role, str(company.id))
    return company, admin_emp, device, token


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py -k "archive or restore or delete" -v
```
Expected: All `FAILED` — endpoints don't exist yet.

- [ ] **Step 3: Add is_deleted guard to _get_device_for_admin**

In `backend/app/api/v1/endpoints/devices.py`, update `_get_device_for_admin`:
```python
async def _get_device_for_admin(device_id: str, admin: Employee, db: AsyncSession) -> Device:
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    result = await db.execute(
        select(Device).where(Device.id == device_uuid, Device.is_deleted == False)  # noqa: E712
    )
    device = result.scalar_one_or_none()
    if not device or device.company_id != admin.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    ids = await visible_device_ids(admin, db)
    if ids is not ALL and device.id not in set(ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device
```

- [ ] **Step 4: Add the three new endpoints to devices.py**

Add the following after the `assign_device` endpoint (around line 123) in `backend/app/api/v1/endpoints/devices.py`:
```python
@router.patch("/{device_id}/archive", response_model=DeviceOut)
async def archive_device(
    device_id: str,
    admin: Employee = Depends(require("devices", "archive")),
    db: AsyncSession = Depends(get_db),
):
    """Suspend a device. It cannot send data until restored."""
    device = await _get_device_for_admin(device_id, admin, db)
    if device.is_archived:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is already archived")

    device.is_active   = False
    device.is_archived = True
    await audit.record(db, admin, "device.archived", "device", str(device.id), {"source": "portal"})
    await db.commit()

    return DeviceOut(
        id=str(device.id),
        hostname=device.hostname,
        platform=device.platform,
        employee_id=str(device.employee_id) if device.employee_id else None,
        employee_name=device.employee.name if device.employee else None,
        last_seen=device.last_seen,
        last_seen_unix=int(device.last_seen.timestamp()) if device.last_seen else None,
        is_online=False,
        is_archived=True,
        is_deleted=False,
    )


@router.patch("/{device_id}/restore", response_model=DeviceOut)
async def restore_device(
    device_id: str,
    admin: Employee = Depends(require("devices", "archive")),
    db: AsyncSession = Depends(get_db),
):
    """Restore an archived device to active status."""
    device = await _get_device_for_admin(device_id, admin, db)
    if not device.is_archived:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is not archived")

    device.is_active   = True
    device.is_archived = False
    await audit.record(db, admin, "device.restored", "device", str(device.id), {"source": "portal"})
    await db.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    return DeviceOut(
        id=str(device.id),
        hostname=device.hostname,
        platform=device.platform,
        employee_id=str(device.employee_id) if device.employee_id else None,
        employee_name=device.employee.name if device.employee else None,
        last_seen=device.last_seen,
        last_seen_unix=int(device.last_seen.timestamp()) if device.last_seen else None,
        is_online=bool(device.last_seen and device.last_seen > cutoff),
        is_archived=False,
        is_deleted=False,
    )


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: str,
    admin: Employee = Depends(require("devices", "delete")),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a device. No DB row is removed; the device disappears from the UI."""
    device = await _get_device_for_admin(device_id, admin, db)

    device.is_deleted  = True
    device.is_active   = False
    device.is_archived = False
    await audit.record(db, admin, "device.deleted", "device", str(device.id), {"source": "portal"})
    await db.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py -k "archive or restore or delete" -v
```
Expected: All `PASSED`

---

## Task 6: Data isolation — scoping + reports

**Files:**
- Modify: `backend/app/core/scoping.py:29-31`
- Modify: `backend/app/api/v1/endpoints/reports.py:48-59` (`_get_visible_device`)
- Modify: `backend/tests/test_device_archive_delete.py` (add new tests)

**Interfaces:**
- Consumes: `Device.is_active`, `Device.is_deleted` from Task 2
- Produces:
  - `visible_device_ids` "own" scope no longer returns archived/deleted device IDs
  - `reports._get_visible_device` returns 404 for deleted devices

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_device_archive_delete.py`:
```python
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
        assert active.id   in id_set,  "Active device must be visible"
        assert archived.id not in id_set, "Archived device must not be visible in own scope"
        assert deleted.id  not in id_set, "Deleted device must not be visible in own scope"

        await db.rollback()


@pytest.mark.asyncio
async def test_deleted_device_returns_404_on_reports(client, db_session):
    """A deleted device returns 404 on device-specific report endpoints."""
    from app.db.admin import ensure_admin
    from app.core.security import create_admin_token

    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"RepTest-{tag}", enrollment_token=f"rtok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin_emp = await ensure_admin(
        db_session, company.id,
        email=f"rep-admin-{tag}@test.io", name="Admin", password="Pass123!"
    )
    await db_session.flush()

    device = Device(
        company_id=company.id, hostname=f"rep-host-{tag}",
        platform="macos", is_active=False, is_deleted=True,
    )
    db_session.add(device)
    await db_session.flush()

    token = create_admin_token(str(admin_emp.id), admin_emp.role, str(company.id))
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get(f"/v1/reports/device/{device.id}/summary?days=7", headers=headers)
    assert r.status_code == 404, "Deleted device summary must return 404"

    r = await client.get(f"/v1/reports/device/{device.id}/top-apps?days=7", headers=headers)
    assert r.status_code == 404, "Deleted device top-apps must return 404"

    await db_session.rollback()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_own_scope_excludes_archived_and_deleted_devices tests/test_device_archive_delete.py::test_deleted_device_returns_404_on_reports -v
```
Expected: Both `FAILED`.

- [ ] **Step 3: Update scoping.py own-scope query**

In `backend/app/core/scoping.py`, replace the `if scope == "own"` block:
```python
    if scope == "own":
        rows = await db.execute(
            select(Device.id).where(
                Device.employee_id == user.id,
                Device.is_active == True,  # noqa: E712
            )
        )
        return [r[0] for r in rows.all()]
```

- [ ] **Step 4: Update reports._get_visible_device**

In `backend/app/api/v1/endpoints/reports.py`, update `_get_visible_device` to add `is_deleted=False`:
```python
async def _get_visible_device(device_id: str, admin: Employee, db: AsyncSession) -> Device:
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    ids = await visible_device_ids(admin, db)
    q = select(Device).where(
        Device.id == device_uuid,
        Device.company_id == admin.company_id,
        Device.is_deleted == False,  # noqa: E712
    )
    if ids is not ALL:
        q = q.where(Device.id.in_(ids))
    device = (await db.execute(q)).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py::test_own_scope_excludes_archived_and_deleted_devices tests/test_device_archive_delete.py::test_deleted_device_returns_404_on_reports -v
```
Expected: Both `PASSED`

- [ ] **Step 6: Run the full new test suite**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_device_archive_delete.py -v
```
Expected: All tests `PASSED`

- [ ] **Step 7: Run the full existing e2e suite to check for regressions**

```bash
docker compose run --rm --no-deps backend python -m pytest tests/test_e2eApplication.py -v
```
Expected: All tests `PASSED` — especially `test_03_device_enrollment` and `test_04_ingest_pipeline` which exercise the modified enroll endpoint and device listing.
