# Role-Based Admin Panel, Groups & Agent Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add fully customizable role-based access control, user management, mixed groups, group-scoped data visibility for Relationship Managers, and admin-managed agent installer distribution with self-update, to the existing TimeTracker stack.

**Architecture:** New Postgres tables (`roles`, `role_permissions`, `groups`, `group_members`, `employee_group_access`, `installer_versions`, `audit_log`) shipped via an idempotent `002_rbac.sql` migration plus a Python seed step (the migration runner splits on `;`, so no `DO $$`/multi-statement SQL). A `require(module, action)` FastAPI dependency gates endpoints; a `visible_device_ids(user)` scoping engine filters every data endpoint by the caller's role scope. The Next.js dashboard gains a `useUser` permission context, role-gated navigation, and Users/Groups/Roles/Agents pages. The Python agent gains an `updater.py` that polls `/v1/updates/check`.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + asyncpg + PostgreSQL; Next.js 14 + TypeScript + SWR + Tailwind; Python agent (httpx, PySide6); MinIO/S3 for installer storage; pytest + httpx for backend scoping tests.

**Spec:** `docs/superpowers/specs/2026-06-03-rbac-admin-panel-design.md`

---

## Testing approach (adaptation note)

This codebase has **no existing test harness**. Verification conventions already in use: `tsc --noEmit`, `next build`, and `docker compose up` smoke checks. This plan keeps those and **adds a minimal pytest harness only for the security-critical permission + scoping logic** (`require()` and `visible_device_ids()`), because scoping bugs leak one RM's data to another. All other surfaces are verified by building and running the stack and exercising endpoints with `curl`/the UI.

The pytest harness uses an in-process SQLite-backed `AsyncSession` for pure-logic tests where possible; scoping queries that use Postgres-specific SQL are tested against the running Postgres service via `docker compose exec`.

---

## File Map

### Backend — new files
| File | Responsibility |
|------|----------------|
| `backend/app/db/migrations/002_rbac.sql` | DDL for all 7 new tables + `employees.role_id` column (idempotent, no `;`-in-statement) |
| `backend/app/core/permissions.py` | `MODULES` catalog constant + helpers (`all_permissions()`, `is_valid(module,action)`) |
| `backend/app/core/scoping.py` | `visible_device_ids(user, db)` scoping engine + `ALL` sentinel |
| `backend/app/db/seed_rbac.py` | Idempotent seed: create Super Admin + Viewer roles per company, link legacy admins |
| `backend/app/schemas/rbac.py` | Pydantic schemas: Role, Permission, UserOut/In, GroupOut/In, MemberIn, InstallerOut, MeOut |
| `backend/app/api/v1/endpoints/roles.py` | `/roles` CRUD + `/roles/modules` |
| `backend/app/api/v1/endpoints/users.py` | `/users` CRUD + `/users/{id}/groups` |
| `backend/app/api/v1/endpoints/groups.py` | `/groups` CRUD + members + CSV import |
| `backend/app/api/v1/endpoints/installers.py` | `/installers` list/upload/activate/download + `/updates/check` |
| `backend/app/core/audit.py` | `record(db, actor, action, target_type, target_id, metadata)` helper |
| `backend/tests/conftest.py` | pytest fixtures (async session, sample company/roles/employees) |
| `backend/tests/test_permissions.py` | `require()` permission-gate tests |
| `backend/tests/test_scoping.py` | `visible_device_ids` scope tests |

### Backend — modified files
| File | Change |
|------|--------|
| `backend/app/db/models.py` | Add `Role`, `RolePermission`, `Group`, `GroupMember`, `EmployeeGroupAccess`, `InstallerVersion`, `AuditLog` models; add `Employee.role_id` + relationships |
| `backend/app/db/base.py` | After migrations, call `seed_rbac.run(...)` |
| `backend/app/api/deps.py` | Add `get_current_user` (loads perms+scope), `require(module,action)` factory |
| `backend/app/api/v1/router.py` | Register `roles`, `users`, `groups`, `installers` routers |
| `backend/app/api/v1/endpoints/auth.py` | Widen login; add `GET /auth/me` |
| `backend/app/api/v1/endpoints/devices.py` | Apply `require('devices','view')` + `visible_device_ids` filter |
| `backend/app/api/v1/endpoints/reports.py` | Apply `require('reports','view')` + scope filter |
| `backend/app/api/v1/endpoints/logs.py` | Apply `require('logs','view')` + scope filter (bound `= ANY(:ids)`) |
| `backend/app/api/v1/endpoints/screenshots.py` | Scope screenshot reads to visible devices |
| `backend/app/core/config.py` | Add `MAX_INSTALLER_MB` setting |
| `backend/requirements.txt` | Add `pytest`, `pytest-asyncio`, `aiosqlite` |

### Frontend — new files
| File | Responsibility |
|------|----------------|
| `dashboard/src/hooks/useUser.tsx` | Context provider + `useUser()` exposing `{name, roleName, scopeType, can()}` |
| `dashboard/src/app/(dashboard)/users/page.tsx` | Users table + Add/Edit modal |
| `dashboard/src/app/(dashboard)/users/_components/UserModal.tsx` | Add/edit user form |
| `dashboard/src/app/(dashboard)/groups/page.tsx` | Groups list + Create/Import |
| `dashboard/src/app/(dashboard)/groups/[id]/page.tsx` | Group detail + members |
| `dashboard/src/app/(dashboard)/groups/_components/GroupModal.tsx` | Create group + CSV import |
| `dashboard/src/app/(dashboard)/groups/[id]/_components/AddMemberModal.tsx` | Member picker (users + devices) |
| `dashboard/src/app/(dashboard)/roles/page.tsx` | Roles table + matrix editor drawer |
| `dashboard/src/app/(dashboard)/roles/_components/RoleEditor.tsx` | Permission-matrix grid + scope selector |
| `dashboard/src/app/(dashboard)/agents/page.tsx` | Installer versions + upload/activate/download |

### Frontend — modified files
| File | Change |
|------|--------|
| `dashboard/src/types/index.ts` | Add `Me`, `Role`, `Permission`, `User`, `Group`, `GroupMember`, `Installer`, `ModuleDef` |
| `dashboard/src/lib/api.ts` | Add `me()`, roles/users/groups/installers API fns; store role on login |
| `dashboard/src/components/Sidebar.tsx` | Add `module` to NAV; filter by `can(module,'view')`; new entries |
| `dashboard/src/app/(dashboard)/layout.tsx` | Wrap in `UserProvider`; move auth gate here |

### Agent — new / modified
| File | Change |
|------|--------|
| `agent/version.py` (new) | `__version__` single source |
| `agent/updater.py` (new) | Poll `/v1/updates/check`, download + run installer |
| `agent/config.py` | Add `UPDATE_CHECK_ENDPOINT`, `AUTO_UPDATE_ENABLED` |
| `agent/main.py` | Start updater thread |
| `agent/enrollment.py` | Import `__version__` from `version.py` |

---

## PHASE 1 — RBAC core

### Task 1.1: Add test harness dependencies

**Files:**
- Modify: `backend/requirements.txt`

- [ ] **Step 1: Append test deps**

Add these lines to `backend/requirements.txt`:
```
pytest==8.2.0
pytest-asyncio==0.23.7
aiosqlite==0.20.0
```

- [ ] **Step 2: Install in the backend container**

Run: `docker compose exec backend pip install pytest==8.2.0 pytest-asyncio==0.23.7 aiosqlite==0.20.0`
Expected: `Successfully installed ...`

- [ ] **Step 3: Commit**
```bash
git add backend/requirements.txt
git commit -m "chore: add pytest harness deps for RBAC tests"
```

### Task 1.2: Migration `002_rbac.sql`

**Files:**
- Create: `backend/app/db/migrations/002_rbac.sql`

- [ ] **Step 1: Write the DDL** (no `DO`/functions; every statement ends in `;`; no inline `;`):

```sql
CREATE TABLE IF NOT EXISTS roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    scope_type  TEXT NOT NULL DEFAULT 'all',
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_roles_company_name ON roles(company_id, name);
CREATE INDEX IF NOT EXISTS idx_roles_company ON roles(company_id);

CREATE TABLE IF NOT EXISTS role_permissions (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    module  TEXT NOT NULL,
    action  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_role_perm ON role_permissions(role_id, module, action);
CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role_id);

ALTER TABLE employees ADD COLUMN IF NOT EXISTS role_id UUID REFERENCES roles(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    created_by  UUID REFERENCES employees(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_groups_company_name ON groups(company_id, name);
CREATE INDEX IF NOT EXISTS idx_groups_company ON groups(company_id);

CREATE TABLE IF NOT EXISTS group_members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id    UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    member_type TEXT NOT NULL,
    employee_id UUID REFERENCES employees(id) ON DELETE CASCADE,
    device_id   UUID REFERENCES devices(id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_member_one_target CHECK ((member_type = 'employee' AND employee_id IS NOT NULL AND device_id IS NULL) OR (member_type = 'device' AND device_id IS NOT NULL AND employee_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_group_employee ON group_members(group_id, employee_id) WHERE employee_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_group_device ON group_members(group_id, device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);

CREATE TABLE IF NOT EXISTS employee_group_access (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id UUID NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    group_id    UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_ega ON employee_group_access(employee_id, group_id);
CREATE INDEX IF NOT EXISTS idx_ega_employee ON employee_group_access(employee_id);

CREATE TABLE IF NOT EXISTS installer_versions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id     UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    platform       TEXT NOT NULL,
    installer_type TEXT NOT NULL,
    version        TEXT NOT NULL,
    s3_key         TEXT NOT NULL,
    file_size      BIGINT NOT NULL DEFAULT 0,
    sha256         TEXT,
    notes          TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT FALSE,
    uploaded_by    UUID REFERENCES employees(id) ON DELETE SET NULL,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_installer_versions_company ON installer_versions(company_id, platform);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_installer ON installer_versions(company_id, platform) WHERE is_active;

CREATE TABLE IF NOT EXISTS audit_log (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        UUID,
    actor_employee_id UUID,
    action            TEXT NOT NULL,
    target_type       TEXT,
    target_id         TEXT,
    metadata          JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_company_time ON audit_log(company_id, created_at DESC);
```

- [ ] **Step 2: Apply by restarting the backend** (its lifespan calls `init_db()`)

Run: `docker compose restart backend && sleep 5 && docker compose logs backend --tail 20`
Expected: log line `Applied migration: 002_rbac.sql` and no error.

- [ ] **Step 3: Verify tables exist**

Run: `docker compose exec db psql -U timetracker -d timetracker -c "\dt"`
Expected: lists `roles`, `role_permissions`, `groups`, `group_members`, `employee_group_access`, `installer_versions`, `audit_log`.

- [ ] **Step 4: Commit**
```bash
git add backend/app/db/migrations/002_rbac.sql
git commit -m "feat(db): RBAC + groups + installer tables migration"
```

### Task 1.3: Permission catalog

**Files:**
- Create: `backend/app/core/permissions.py`

- [ ] **Step 1: Write the catalog**
```python
"""Single source of truth for the RBAC permission matrix."""
from typing import TypedDict


class ModuleDef(TypedDict):
    key: str
    label: str
    actions: list[str]


MODULES: list[ModuleDef] = [
    {"key": "devices",     "label": "Devices",       "actions": ["view"]},
    {"key": "logs",        "label": "Activity Logs", "actions": ["view"]},
    {"key": "reports",     "label": "Reports",       "actions": ["view"]},
    {"key": "screenshots", "label": "Screenshots",   "actions": ["view"]},
    {"key": "users",       "label": "Users",         "actions": ["view", "create", "edit", "delete"]},
    {"key": "groups",      "label": "Groups",        "actions": ["view", "create", "edit", "delete"]},
    {"key": "roles",       "label": "Roles",         "actions": ["view", "create", "edit", "delete"]},
    {"key": "agents",      "label": "Agents",        "actions": ["view", "create", "edit", "delete"]},
    {"key": "settings",    "label": "Settings",      "actions": ["view", "edit"]},
    {"key": "audit",       "label": "Audit Log",     "actions": ["view"]},
]

_VALID: set[tuple[str, str]] = {
    (m["key"], a) for m in MODULES for a in m["actions"]
}


def all_permissions() -> list[tuple[str, str]]:
    """Every (module, action) pair — used to seed Super Admin."""
    return sorted(_VALID)


def is_valid(module: str, action: str) -> bool:
    return (module, action) in _VALID
```

- [ ] **Step 2: Commit**
```bash
git add backend/app/core/permissions.py
git commit -m "feat: RBAC permission catalog"
```

### Task 1.4: ORM models for new tables

**Files:**
- Modify: `backend/app/db/models.py`

- [ ] **Step 1: Add models** (append after existing models; add `role_id` + relationship to `Employee`):
```python
class Role(Base):
    __tablename__ = "roles"
    id:          Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:  Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("companies.id"))
    name:        Mapped[str]       = mapped_column(String, nullable=False)
    description: Mapped[str | None]= mapped_column(Text, nullable=True)
    scope_type:  Mapped[str]       = mapped_column(String, default="all")
    is_system:   Mapped[bool]      = mapped_column(Boolean, default=False)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    permissions: Mapped[list["RolePermission"]] = relationship(back_populates="role", cascade="all, delete-orphan")


class RolePermission(Base):
    __tablename__ = "role_permissions"
    id:      Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("roles.id"))
    module:  Mapped[str]       = mapped_column(String, nullable=False)
    action:  Mapped[str]       = mapped_column(String, nullable=False)
    role:    Mapped["Role"]    = relationship(back_populates="permissions")


class Group(Base):
    __tablename__ = "groups"
    id:          Mapped[uuid.UUID]      = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:  Mapped[uuid.UUID]      = mapped_column(UUID, ForeignKey("companies.id"))
    name:        Mapped[str]            = mapped_column(String, nullable=False)
    description: Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_by:  Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    created_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    members:     Mapped[list["GroupMember"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"
    id:          Mapped[uuid.UUID]      = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    group_id:    Mapped[uuid.UUID]      = mapped_column(UUID, ForeignKey("groups.id"))
    member_type: Mapped[str]            = mapped_column(String, nullable=False)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    device_id:   Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("devices.id"), nullable=True)
    added_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    group:       Mapped["Group"]        = relationship(back_populates="members")


class EmployeeGroupAccess(Base):
    __tablename__ = "employee_group_access"
    id:          Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("employees.id"))
    group_id:    Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("groups.id"))


class InstallerVersion(Base):
    __tablename__ = "installer_versions"
    id:             Mapped[uuid.UUID]      = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:     Mapped[uuid.UUID]      = mapped_column(UUID, ForeignKey("companies.id"))
    platform:       Mapped[str]            = mapped_column(String, nullable=False)
    installer_type: Mapped[str]            = mapped_column(String, nullable=False)
    version:        Mapped[str]            = mapped_column(String, nullable=False)
    s3_key:         Mapped[str]            = mapped_column(Text, nullable=False)
    file_size:      Mapped[int]            = mapped_column(BigInteger, default=0)
    sha256:         Mapped[str | None]     = mapped_column(String, nullable=True)
    notes:          Mapped[str | None]     = mapped_column(Text, nullable=True)
    is_active:      Mapped[bool]           = mapped_column(Boolean, default=False)
    uploaded_by:    Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    uploaded_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id:                Mapped[uuid.UUID]      = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:        Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    action:            Mapped[str]            = mapped_column(String, nullable=False)
    target_type:       Mapped[str | None]     = mapped_column(String, nullable=True)
    target_id:         Mapped[str | None]     = mapped_column(String, nullable=True)
    audit_metadata:    Mapped[dict | None]    = mapped_column("metadata", JSON, nullable=True)
    created_at:        Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
```

- [ ] **Step 2: Add `role_id` + relationship to the existing `Employee` class** (inside the class body):
```python
    role_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("roles.id"), nullable=True)
    role_obj: Mapped["Role | None"]   = relationship(lazy="joined")
```

- [ ] **Step 3: Ensure imports at top of `models.py` include** `BigInteger`, `Text`, `JSON`, `Boolean` from `sqlalchemy` (add any missing).

- [ ] **Step 4: Verify the app imports cleanly**

Run: `docker compose restart backend && sleep 5 && docker compose logs backend --tail 15`
Expected: backend healthy, no import/mapper errors.

- [ ] **Step 5: Commit**
```bash
git add backend/app/db/models.py
git commit -m "feat(db): ORM models for roles, groups, installers, audit"
```

### Task 1.5: Seed Super Admin + link legacy admins

**Files:**
- Create: `backend/app/db/seed_rbac.py`
- Modify: `backend/app/db/base.py`

- [ ] **Step 1: Write the seed** (idempotent — guard on role name):
```python
"""Idempotent RBAC seed: ensure each company has system roles and that legacy
admins/managers are linked to a Super Admin role."""
import logging
from sqlalchemy import select
from app.db.base import AsyncSessionLocal
from app.db.models import Company, Employee, Role, RolePermission
from app.core.permissions import all_permissions

log = logging.getLogger(__name__)


async def run() -> None:
    async with AsyncSessionLocal() as db:
        companies = (await db.execute(select(Company))).scalars().all()
        for company in companies:
            sa = (await db.execute(
                select(Role).where(Role.company_id == company.id, Role.name == "Super Admin")
            )).scalar_one_or_none()
            if not sa:
                sa = Role(company_id=company.id, name="Super Admin", scope_type="all",
                          is_system=True, description="Full access to everything")
                db.add(sa)
                await db.flush()
                for module, action in all_permissions():
                    db.add(RolePermission(role_id=sa.id, module=module, action=action))
                log.info("Seeded Super Admin role for company %s", company.name)

            viewer = (await db.execute(
                select(Role).where(Role.company_id == company.id, Role.name == "Viewer")
            )).scalar_one_or_none()
            if not viewer:
                viewer = Role(company_id=company.id, name="Viewer", scope_type="groups",
                              is_system=False, description="Read-only, group-scoped")
                db.add(viewer)
                await db.flush()
                for module in ("devices", "logs", "reports"):
                    db.add(RolePermission(role_id=viewer.id, module=module, action="view"))

            # Link legacy admins/managers with no role_id to Super Admin
            legacy = (await db.execute(
                select(Employee).where(
                    Employee.company_id == company.id,
                    Employee.role_id.is_(None),
                    Employee.role.in_(("admin", "manager")),
                )
            )).scalars().all()
            for emp in legacy:
                emp.role_id = sa.id
                log.info("Linked legacy admin %s to Super Admin", emp.email)
        await db.commit()
```

- [ ] **Step 2: Call seed after migrations in `base.py`** — at the end of `init_db()`:
```python
    # after the migration loop
    from app.db import seed_rbac
    await seed_rbac.run()
    log.info("RBAC seed complete")
```

- [ ] **Step 3: Verify**

Run: `docker compose restart backend && sleep 6 && docker compose exec db psql -U timetracker -d timetracker -c "SELECT name, scope_type, is_system FROM roles;"`
Expected: rows for `Super Admin` (all, t) and `Viewer` (groups, f).

Run: `docker compose exec db psql -U timetracker -d timetracker -c "SELECT email, role_id FROM employees WHERE role='admin';"`
Expected: seeded admin has a non-null `role_id`.

- [ ] **Step 4: Commit**
```bash
git add backend/app/db/seed_rbac.py backend/app/db/base.py
git commit -m "feat(db): seed Super Admin/Viewer roles, link legacy admins"
```

### Task 1.6: `get_current_user` + `require()` dependency (with tests)

**Files:**
- Modify: `backend/app/api/deps.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_permissions.py`

- [ ] **Step 1: Write the failing test** `backend/tests/test_permissions.py`:
```python
import pytest
from app.core.permissions import is_valid, all_permissions


def test_catalog_has_core_modules():
    assert is_valid("users", "create")
    assert is_valid("devices", "view")
    assert not is_valid("users", "explode")
    assert ("roles", "delete") in all_permissions()


@pytest.mark.parametrize("perms,module,action,allowed", [
    ({("users", "view")}, "users", "view", True),
    ({("users", "view")}, "users", "delete", False),
    (set(), "devices", "view", False),
])
def test_permission_check(perms, module, action, allowed):
    from app.api.deps import _has_permission
    assert _has_permission(perms, module, action) is allowed
```

- [ ] **Step 2: Run it, expect failure**

Run: `docker compose exec backend pytest tests/test_permissions.py -v`
Expected: FAIL — `_has_permission` does not exist yet.

- [ ] **Step 3: Implement in `deps.py`**:
```python
from app.db.models import Employee, Role, RolePermission
from sqlalchemy.orm import selectinload


def _has_permission(perms: set[tuple[str, str]], module: str, action: str) -> bool:
    return (module, action) in perms


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> Employee:
    try:
        payload = decode_token(creds.credentials)
        if payload.get("type") != "admin":
            raise ValueError
        user_uuid = uuid.UUID(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(
        select(Employee)
        .options(selectinload(Employee.role_obj).selectinload(Role.permissions))
        .where(Employee.id == user_uuid, Employee.is_active == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Compute effective permissions + scope
    if user.role_obj:
        user._perms = {(p.module, p.action) for p in user.role_obj.permissions}
        user._scope_type = user.role_obj.scope_type
    elif user.role in ("admin", "manager"):   # legacy fallback
        from app.core.permissions import all_permissions
        user._perms = set(all_permissions())
        user._scope_type = "all"
    else:
        user._perms = set()
        user._scope_type = "own"
    return user


def require(module: str, action: str):
    async def _dep(user: Employee = Depends(get_current_user)) -> Employee:
        if not _has_permission(getattr(user, "_perms", set()), module, action):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Missing permission: {module}.{action}")
        return user
    return _dep
```
Keep the existing `get_current_admin` as an alias: `get_current_admin = get_current_user` (so unmodified endpoints keep working until updated).

- [ ] **Step 4: Write `conftest.py`** (minimal — supports the pure-logic tests above; no DB needed for them):
```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest

pytest_plugins = ("pytest_asyncio",)
```

- [ ] **Step 5: Run tests, expect pass**

Run: `docker compose exec backend pytest tests/test_permissions.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**
```bash
git add backend/app/api/deps.py backend/tests/
git commit -m "feat(auth): get_current_user with permissions + require() gate"
```

### Task 1.7: Roles endpoints + `/auth/me` + widened login

**Files:**
- Create: `backend/app/schemas/rbac.py`
- Create: `backend/app/api/v1/endpoints/roles.py`
- Modify: `backend/app/api/v1/endpoints/auth.py`
- Modify: `backend/app/api/v1/router.py`

- [ ] **Step 1: Schemas** in `rbac.py`:
```python
from pydantic import BaseModel
from typing import Optional


class PermissionItem(BaseModel):
    module: str
    action: str


class RoleIn(BaseModel):
    name: str
    description: Optional[str] = None
    scope_type: str = "all"        # all | groups | own
    permissions: list[PermissionItem] = []


class RoleOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    scope_type: str
    is_system: bool
    permissions: list[PermissionItem]


class MeOut(BaseModel):
    id: str
    name: str
    email: str
    role_name: Optional[str]
    scope_type: str
    permissions: list[PermissionItem]
```

- [ ] **Step 2: Roles router** `roles.py` — `GET /modules`, `GET /`, `POST /`, `GET/PATCH/DELETE /{id}`. Validates each permission via `permissions.is_valid`; `DELETE` returns 409 if `is_system` or referenced by any employee. Use `require('roles', <action>)`. (Full CRUD with `record()` audit calls — see audit task; until then omit audit.)

- [ ] **Step 3: `/auth/me`** in `auth.py`:
```python
@router.get("/me", response_model=MeOut)
async def me(user: Employee = Depends(get_current_user)):
    return MeOut(
        id=str(user.id), name=user.name, email=user.email,
        role_name=user.role_obj.name if user.role_obj else user.role,
        scope_type=getattr(user, "_scope_type", "own"),
        permissions=[PermissionItem(module=m, action=a) for (m, a) in sorted(getattr(user, "_perms", set()))],
    )
```

- [ ] **Step 4: Widen login** in `auth.py` — replace the `role not in ('admin','manager')` gate with: allow if `employee.is_active and employee.password_hash and (employee.role_id is not None or employee.role in ('admin','manager'))`. Return `role_name` = role_obj.name or legacy role.

- [ ] **Step 5: Register routers** in `router.py`:
```python
from app.api.v1.endpoints import roles
api_router.include_router(roles.router, prefix="/roles", tags=["roles"])
```

- [ ] **Step 6: Verify**

Run: get a token, then
`curl -s localhost:8000/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq`
Expected: `scope_type: "all"` and a full `permissions` array for the seeded admin.
`curl -s localhost:8000/v1/roles/modules -H "Authorization: Bearer $TOKEN" | jq '.[0]'`
Expected: `{key:"devices",...}`.

- [ ] **Step 7: Commit**
```bash
git add backend/app/schemas/rbac.py backend/app/api/v1/endpoints/roles.py backend/app/api/v1/endpoints/auth.py backend/app/api/v1/router.py
git commit -m "feat(api): roles CRUD, /auth/me, widened login"
```

### Task 1.8: Gate existing endpoints with `require()`

**Files:**
- Modify: `devices.py`, `reports.py`, `logs.py`, `screenshots.py`

- [ ] **Step 1:** In each endpoint, change the dependency from `Depends(get_current_admin)` to `Depends(require('<module>', 'view'))` where module is `devices`/`reports`/`logs`/`screenshots`. (Scoping filter is added in Phase 2 — for now only the permission gate.)

- [ ] **Step 2: Verify** the seeded admin (full perms) still reads every endpoint:
Run: `curl -s localhost:8000/v1/devices -H "Authorization: Bearer $TOKEN" | jq 'length'`
Expected: device count, HTTP 200.

- [ ] **Step 3: Commit**
```bash
git add backend/app/api/v1/endpoints/devices.py backend/app/api/v1/endpoints/reports.py backend/app/api/v1/endpoints/logs.py backend/app/api/v1/endpoints/screenshots.py
git commit -m "feat(api): gate data endpoints with require() permission"
```

---

## PHASE 2 — Scoping engine + Groups

### Task 2.1: `visible_device_ids` scoping engine

**Files:**
- Create: `backend/app/core/scoping.py`

- [ ] **Step 1: Implement**:
```python
"""Single source of truth for which devices a user may see."""
import uuid
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Device, GroupMember, EmployeeGroupAccess

ALL = object()  # sentinel: no device-level filter (company filter still applies)


async def visible_device_ids(user, db: AsyncSession):
    scope = getattr(user, "_scope_type", "own")
    if scope == "all":
        return ALL
    if scope == "own":
        rows = await db.execute(select(Device.id).where(Device.employee_id == user.id))
        return [r[0] for r in rows.all()]
    # scope == "groups"
    gids = (await db.execute(
        select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == user.id)
    )).scalars().all()
    if not gids:
        return []
    direct = await db.execute(
        select(GroupMember.device_id).where(
            GroupMember.group_id.in_(gids), GroupMember.member_type == "device"
        )
    )
    via_emp = await db.execute(text("""
        SELECT d.id FROM devices d
        JOIN group_members gm ON gm.employee_id = d.employee_id
        WHERE gm.group_id = ANY(:gids) AND gm.member_type = 'employee'
    """), {"gids": [str(g) for g in gids]})
    ids = {r[0] for r in direct.all() if r[0]} | {r[0] for r in via_emp.all()}
    return list(ids)
```

- [ ] **Step 2: Commit**
```bash
git add backend/app/core/scoping.py
git commit -m "feat(scoping): visible_device_ids engine"
```

### Task 2.2: Wire scoping into data endpoints

**Files:** `devices.py`, `reports.py`, `logs.py`, `screenshots.py`

- [ ] **Step 1:** In each list/query endpoint, after resolving the user, call `ids = await visible_device_ids(user, db)`. If `ids is ALL`, keep current company-only query. Else apply:
  - SQLAlchemy select: `.where(Device.id.in_(ids))` (devices) / `ActivityEvent.device_id.in_(ids)`.
  - raw SQL (logs/reports): append `AND d.id = ANY(:vids)` / `AND ae.device_id = ANY(:vids)` and bind `params["vids"] = [str(i) for i in ids]`.
  - Empty list → return `[]` immediately (RM with no groups).
- For `_get_device_for_admin`, also 404 if the resolved device id ∉ ids (when not ALL).

- [ ] **Step 2: Verify** (after Phase 2 groups exist, re-verified in Task 2.5). For now, admin (`ALL`) still returns everything:
Run: `curl -s localhost:8000/v1/logs -H "Authorization: Bearer $TOKEN" | jq 'length'`
Expected: 200 + events.

- [ ] **Step 3: Commit**
```bash
git add backend/app/api/v1/endpoints/devices.py backend/app/api/v1/endpoints/reports.py backend/app/api/v1/endpoints/logs.py backend/app/api/v1/endpoints/screenshots.py
git commit -m "feat(api): scope data endpoints to visible devices"
```

### Task 2.3: Groups endpoints + members + CSV import

**Files:**
- Create: `backend/app/api/v1/endpoints/groups.py`
- Modify: `backend/app/api/v1/router.py`, `backend/app/schemas/rbac.py`

- [ ] **Step 1: Schemas** (`GroupIn`, `GroupOut` with `member_count`, `GroupDetailOut` with members, `MemberIn{member_type, employee_id?, device_id?}`, `MemberOut`).
- [ ] **Step 2: Endpoints** — `GET /` (list with member counts, company-scoped), `POST /` (create; 409 on dup name), `GET /{id}` (detail + members joined to employee name / device hostname), `PATCH /{id}` (rename), `DELETE /{id}`, `GET/POST /{id}/members`, `DELETE /{id}/members/{member_id}`, `POST /import` (multipart CSV: columns `group,email_or_hostname,type`; create missing groups; resolve employee by email or device by hostname within company; return `{created, added, skipped:[{row,reason}]}`). All gated by `require('groups', <action>)`.
- [ ] **Step 3: Register** router with prefix `/groups`.
- [ ] **Step 4: Verify**
```
curl -X POST localhost:8000/v1/groups -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"name":"Dental"}'
curl -s localhost:8000/v1/groups -H "Authorization: Bearer $TOKEN" | jq
```
Expected: group created, appears in list with `member_count: 0`.
- [ ] **Step 5: Commit**
```bash
git add backend/app/api/v1/endpoints/groups.py backend/app/api/v1/router.py backend/app/schemas/rbac.py
git commit -m "feat(api): groups CRUD, members, CSV import"
```

### Task 2.4: Scope test (security-critical)

**Files:**
- Create: `backend/tests/test_scoping.py`

- [ ] **Step 1: Write a Postgres-backed scope test** that, against the running DB, creates a company, two groups, two employees with devices, an RM with access to group A only, and asserts `visible_device_ids(rm, db)` returns exactly group A's devices and never group B's. Run via:
Run: `docker compose exec backend pytest tests/test_scoping.py -v`
Expected: PASS.
- [ ] **Step 2: Commit**
```bash
git add backend/tests/test_scoping.py
git commit -m "test(scoping): RM sees only assigned groups"
```

### Task 2.5: End-to-end scope verification

- [ ] **Step 1:** Create a "groups"-scope role, a test RM user (Phase 3 has the UI; here use SQL/`curl`), assign group A, log in as the RM, and confirm `/v1/devices`, `/v1/logs`, `/v1/reports/company/summary` return only group A's devices. Document the curl sequence in the commit message. (No code; verification gate.)

---

## PHASE 3 — User management

### Task 3.1: Users endpoints

**Files:**
- Create: `backend/app/api/v1/endpoints/users.py`
- Modify: `backend/app/api/v1/router.py`, `backend/app/schemas/rbac.py`

- [ ] **Step 1: Schemas** — `UserIn{name,email,role_id?,password?,is_monitored?,group_ids:[]}`, `UserOut{id,name,email,role_id,role_name,scope_type,is_active,group_ids,device_count}`, `UserPatch` (all optional incl. `password`, `is_active`).
- [ ] **Step 2: Endpoints** — `GET /` (search by name/email, filter by role/active; company-scoped), `POST /` (create; hash password if given; set role_id; write `employee_group_access` from group_ids; 409 on dup email), `GET /{id}`, `PATCH /{id}` (edit fields, reset password, activate/deactivate), `DELETE /{id}` (soft: `is_active=False`), `PUT /{id}/groups` (replace `employee_group_access`). Gated by `require('users', <action>)`.
- [ ] **Step 3: Register** router `/users`.
- [ ] **Step 4: Verify** — create RM via curl, GET list shows them, PATCH role works.
- [ ] **Step 5: Commit**
```bash
git add backend/app/api/v1/endpoints/users.py backend/app/api/v1/router.py backend/app/schemas/rbac.py
git commit -m "feat(api): users CRUD + group assignment"
```

---

## PHASE 4 — Agent management + self-update

### Task 4.1: Installer storage settings + endpoints

**Files:**
- Modify: `backend/app/core/config.py` (add `MAX_INSTALLER_MB: int = 600`)
- Create: `backend/app/api/v1/endpoints/installers.py`
- Modify: `backend/app/api/v1/router.py`, `backend/app/schemas/rbac.py`

- [ ] **Step 1: Schemas** — `InstallerOut{id,platform,installer_type,version,file_size,sha256,notes,is_active,uploaded_at}`.
- [ ] **Step 2: Endpoints** (reuse `screenshots._s3_client`):
  - `GET /` list versions (company-scoped, newest first), `require('agents','view')`.
  - `POST /` multipart upload (`platform`, `installer_type`, `version`, `notes`, `file`): validate extension↔type and size ≤ `MAX_INSTALLER_MB`; compute sha256; PUT to `installers/{company}/{platform}/{version}/TimeTracker.{ext}`; insert row (`is_active=False`). `require('agents','create')`.
  - `POST /{id}/activate`: in a transaction, set all other rows for (company, platform) `is_active=False`, set this one `True`. `require('agents','edit')`.
  - `GET /{id}/download`: presigned GET URL (3600s). `require('agents','view')`.
- [ ] **Step 3: `GET /updates/check`** (device-auth via `get_current_device`): find active installer for `device.platform`; if its `version` != `device.agent_version`, return `{update_available:true, version, download_url}` else `{update_available:false}`.
- [ ] **Step 4: Register** router `/installers` and add the `/updates` route (either in installers router with full path or a tiny `updates.py`). Use prefix `/installers`; mount updates at `/updates`.
- [ ] **Step 5: Verify** — upload a small dummy file as `macos/dmg/1.0.1`, activate, `GET /updates/check` as a device token returns the update.
- [ ] **Step 6: Commit**
```bash
git add backend/app/core/config.py backend/app/api/v1/endpoints/installers.py backend/app/api/v1/router.py backend/app/schemas/rbac.py
git commit -m "feat(api): installer upload/download/activate + update check"
```

### Task 4.2: Agent self-update

**Files:**
- Create: `agent/version.py` (`__version__ = "1.0.0"`)
- Create: `agent/updater.py`
- Modify: `agent/config.py`, `agent/main.py`, `agent/enrollment.py`

- [ ] **Step 1:** `config.py` — add `UPDATE_CHECK_ENDPOINT = f"{API_BASE}/v1/updates/check"` and `AUTO_UPDATE_ENABLED = True`.
- [ ] **Step 2:** `enrollment.py` — `from version import __version__` (replace the local literal).
- [ ] **Step 3:** `updater.py` — a `run(shared)` loop: every 6h (and once at start, after a short delay), GET `UPDATE_CHECK_ENDPOINT` with the device JWT; if `update_available` and remote semver > local, download to temp; macOS → `hdiutil attach` + copy `.app` to `/Applications` + `hdiutil detach`; Windows → `subprocess.run(["msiexec","/i",path,"/quiet","/qn","/norestart"])`. Wrap in try/except; never crash the agent.
- [ ] **Step 4:** `main.py` — start `UpdateWorker` as a daemon thread when `Config.AUTO_UPDATE_ENABLED`.
- [ ] **Step 5: Verify** — run agent locally against backend with an activated newer version; confirm it logs "update available" and attempts download. (Full install verified manually on each OS.)
- [ ] **Step 6: Commit**
```bash
git add agent/version.py agent/updater.py agent/config.py agent/main.py agent/enrollment.py
git commit -m "feat(agent): self-update via /updates/check"
```

---

## PHASE 5 — Frontend

### Task 5.1: Types + API client + login role capture

**Files:** `dashboard/src/types/index.ts`, `dashboard/src/lib/api.ts`

- [ ] **Step 1: Types** — add `ModuleDef`, `Permission{module,action}`, `Me{id,name,email,role_name,scope_type,permissions}`, `Role`, `User`, `Group`, `GroupMember`, `Installer`.
- [ ] **Step 2: API fns** — `me()`, `getRoles/createRole/updateRole/deleteRole/getModules`, `getUsers/createUser/updateUser/deleteUser/setUserGroups`, `getGroups/createGroup/updateGroup/deleteGroup/getGroup/addMember/removeMember/importGroupsCsv`, `getInstallers/uploadInstaller/activateInstaller/downloadInstaller`.
- [ ] **Step 3: Verify** `tsc --noEmit` clean.
- [ ] **Step 4: Commit** `feat(fe): RBAC types + API client`.

### Task 5.2: `useUser` context + gated layout/sidebar

**Files:** `dashboard/src/hooks/useUser.tsx`, `dashboard/src/app/(dashboard)/layout.tsx`, `dashboard/src/components/Sidebar.tsx`

- [ ] **Step 1:** `useUser.tsx` — `UserProvider` fetches `me()` once, stores `{name, roleName, scopeType, can(module,action)}`; `can` returns true if `scope_type==='all'` OR perm present; redirect to `/login` on failure.
- [ ] **Step 2:** Wrap dashboard `layout.tsx` children in `UserProvider`; move the auth gate here.
- [ ] **Step 3:** `Sidebar.tsx` — add `module` to each NAV entry; add Users/Groups/Roles/Agents; render `NAV.filter(n => !n.module || can(n.module,'view'))`.
- [ ] **Step 4: Verify** `next build` clean; admin sees all nav.
- [ ] **Step 5: Commit** `feat(fe): permission context + gated navigation`.

### Task 5.3: Roles page

**Files:** `roles/page.tsx`, `roles/_components/RoleEditor.tsx`

- [ ] **Step 1:** Table of roles (name, scope, #permissions, system badge). "New role" + edit open `RoleEditor`: name, description, scope radio (all/groups/own), and a matrix grid built from `getModules()` (rows=modules, cols=actions, checkboxes). Save calls create/update. Delete blocked for `is_system` (hide button) and shows 409 toast otherwise.
- [ ] **Step 2: Verify** create a "Dental RM" role (groups scope, devices/logs/reports view) via UI; appears in list.
- [ ] **Step 3: Commit** `feat(fe): roles page with permission matrix`.

### Task 5.4: Users page

**Files:** `users/page.tsx`, `users/_components/UserModal.tsx`

- [ ] **Step 1:** Searchable table (name, email, role, status, #devices). "Add User" modal: name, email, role select, optional password, monitored toggle, and a group multiselect shown when the chosen role scope==='groups'. Row actions: edit, deactivate, reset password. Save writes user + groups.
- [ ] **Step 2: Verify** create an RM, assign the Dental role + a group, via UI.
- [ ] **Step 3: Commit** `feat(fe): users page + add/edit modal`.

### Task 5.5: Groups pages

**Files:** `groups/page.tsx`, `groups/[id]/page.tsx`, `groups/_components/GroupModal.tsx`, `groups/[id]/_components/AddMemberModal.tsx`

- [ ] **Step 1:** `groups/page.tsx` — "Create Group" (modal: New Group name/desc, or Upload CSV) + table (name, members, created). Row → detail.
- [ ] **Step 2:** `groups/[id]/page.tsx` — info card (name, member count, created) + members table (name/hostname, type) + "Add Member" + remove.
- [ ] **Step 3:** `AddMemberModal` — search users and devices, add by type.
- [ ] **Step 4: Verify** create group, add a user + a device, CSV import a small file, via UI.
- [ ] **Step 5: Commit** `feat(fe): groups list, detail, members, CSV import`.

### Task 5.6: Agents page

**Files:** `agents/page.tsx`

- [ ] **Step 1:** Active version card per OS; drag-drop/upload form (platform, type, version, notes, file); version-history table with Activate + Download buttons.
- [ ] **Step 2: Verify** upload a dummy installer, activate, download link resolves.
- [ ] **Step 3: Commit** `feat(fe): agents installer management page`.

---

## PHASE 6 — Audit log + end-to-end

### Task 6.1: Audit helper + writes

**Files:** `backend/app/core/audit.py`; modify mutating endpoints (roles, users, groups, installers).

- [ ] **Step 1:** `audit.py` — `async def record(db, actor, action, target_type=None, target_id=None, metadata=None)` inserts an `AuditLog` row (best-effort; never raises into the request).
- [ ] **Step 2:** Call `record(...)` after each create/update/delete/activate in roles/users/groups/installers.
- [ ] **Step 3:** `GET /v1/audit` endpoint (`require('audit','view')`), newest-first, company-scoped, limit/paged.
- [ ] **Step 4: Verify** a create shows up in `GET /v1/audit`.
- [ ] **Step 5: Commit** `feat: audit log writes + viewer endpoint`.

### Task 6.2: Audit page (optional UI) + full walkthrough

- [ ] **Step 1:** `audit/page.tsx` — table of recent actions (gated by `audit.view`); add to sidebar.
- [ ] **Step 2: Full end-to-end:** as Super Admin create role → create RM user → create group → add members → log in as RM → confirm scoped data in devices/logs/reports and that Users/Roles/Agents nav is hidden. Upload + activate installer; agent update-check returns it.
- [ ] **Step 3: Verify** `tsc --noEmit` + `next build` clean; `docker compose up` healthy.
- [ ] **Step 4: Commit** `feat(fe): audit page + e2e verified`.

---

## Self-Review

**Spec coverage:** Users (P3, 5.4) ✓; Groups incl. create/list/detail/members/CSV (P2 2.3, 5.5) ✓; Roles + matrix + multiple admins (P1 1.7, 5.3) ✓; module-access visibility (require gate P1 1.8) ✓; RM group-scoping core purpose (P2 scoping engine + wiring + tests) ✓; installer download + admin upload + self-update (P4) ✓; nothing-hardcoded (modules catalog, data-driven roles/groups) ✓; multiple admins (any user with role) ✓; audit/accountability (P6) ✓.

**Placeholder scan:** Concrete code given for migration, models, permissions, seed, deps, scoping, `/me`. CRUD endpoints (roles/users/groups/installers) and frontend pages are specified by exact fields, routes, permissions, and verification rather than full code — these follow one repeated, well-defined pattern (FastAPI router + Pydantic schema + SWR page) already demonstrated in 1.7/2.1; acceptable for an experienced implementer and kept DRY.

**Type consistency:** `get_current_user`/`require()` (1.6) used everywhere after; `visible_device_ids`/`ALL` sentinel (2.1) consumed identically in 2.2; `MeOut`/`PermissionItem` (1.7) match frontend `Me`/`Permission` (5.1); `can(module,action)` (5.2) matches the `(module,action)` permission shape throughout; `audit_metadata` ORM attr maps to the `metadata` column (reserved-name safe).

**Migration-runner safety:** `002_rbac.sql` uses only single statements terminated by `;`, no `DO $$` blocks, no inline `;` — seeding is in Python (`seed_rbac.py`), satisfying the `split(';')` runner.
