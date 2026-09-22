# Talent/RM Guardrails, Users Split, Offline Alerts, Subgroup Members — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the four features approved in `docs/superpowers/specs/2026-07-06-talent-rm-groups-and-offline-alerts-design.md`: (1) Talent users can never be granted group-view (Relation Manager) access, (2) the Users page splits Team from Talent via tabs, (3) Super Admins and Relation Managers get an in-app alert when a talent's device has been offline past a configurable threshold, (4) a group's detail page can show members inherited from its subgroups.

**Architecture:** All four features extend existing FastAPI endpoints/SQLAlchemy models and existing Next.js pages/components — no new tables, no SQL migration, no email subsystem, no new background worker. Feature 1 adds a single guard inside the existing `_set_group_access` helper (the one choke point all group-access writes flow through). Feature 2 adds a computed `user_kind` field to the users API. Feature 3 extends the existing computed notification feed (`notifications.py`) using the existing `visible_device_ids` scoping helper and the existing (previously unused) `offline_alert_mins` company setting. Feature 4 adds an `include_subgroups` query param to the existing group-members code path.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + PostgreSQL (raw `.sql` migrations — none needed here). Next.js App Router + TypeScript + SWR + Tailwind + lucide-react. Backend tests: pytest + pytest-asyncio against a live Postgres, run inside Docker. Frontend has no unit-test framework configured.

## Global Constraints

- No SQL migration in this plan — all new persisted state (`offline_alert_mins` default change, new `offline_alert_enabled` key) lives in the existing `companies.settings` JSONB column; all other additions (`user_kind`, `MemberOut` extra fields) are response-schema-only, computed on read.
- No email/SMTP delivery — all alerts are in-app only, via the existing computed notification feed pattern.
- Do not start the pre-existing-but-dormant `device_janitor` / `report_cache` / `tech_cache` workers — out of scope for this plan.
- The Talent/RM guard must never block changing a user's role away from Talent — including in the same request that also assigns group access.
- Backend tests run via Docker (local Python 3.13 cannot build the pinned deps): `docker compose up -d db` once, then `docker compose run --rm --no-deps backend python -m pytest tests/<file> -v` per task.
- Frontend has no Jest/Vitest configured — verify frontend tasks with `npm run build` (runs Next.js's type check) from `dashboard/`, plus a manual browser smoke-check where noted. Do not add a test framework as part of this plan.
- Follow existing conventions: backend files/functions stay snake_case (Python), frontend components stay PascalCase/camelCase (TSX) — this plan creates no new source files, only new `test_*.py` files matching the existing `backend/tests/` convention.
- Commit messages: short, plain, capitalized sentences with no type prefix (matches this repo's existing log, e.g. "Report queue added", "redis logs implemented") — do not use conventional-commit prefixes like `feat:`/`fix:`.

---

## Task 1: Talent/RM guard on group-access assignment (Feature 1, backend)

Every place an employee's group-view access is written funnels through one helper, `_set_group_access`, in `backend/app/api/v1/endpoints/users.py`. This task adds a shared `is_talent_role` check there so a Talent user (or anyone without a `groups`-scoped role) can never be granted group access — while never blocking a role change, since every call site already resolves the *final* role for the request before calling this helper.

**Files:**
- Modify: `backend/app/core/provisioning.py:1-96` (add `is_talent_role` helper)
- Modify: `backend/app/api/v1/endpoints/users.py:1-25` (import), `:66-81` (`_set_group_access`), `:458-493` (`create_user`), `:528-563` (`update_user`), `:566-576` (`set_user_groups`), `:579-606` (`approve_user`)
- Test: Create `backend/tests/test_talent_rm_guard.py`

**Interfaces:**
- Consumes: `Role` model (`backend/app/db/models.py:123-133`, fields `id`, `name`, `scope_type`, `is_system`); `TALENT_ROLE_NAME` constant (`provisioning.py:12`); `client`/`db_session` pytest fixtures (`backend/tests/conftest.py`); `create_admin_token(user_id, role, company_id)` (`backend/app/core/security.py:50`).
- Produces: `is_talent_role(role: Role | None) -> bool` in `app.core.provisioning`, imported by Tasks 2 and 5. `_set_group_access` new signature `_set_group_access(emp_id, company_id, group_ids, role, db)` — callers elsewhere must pass the resolved `Role | None`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_talent_rm_guard.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose up -d db && docker compose run --rm --no-deps backend python -m pytest tests/test_talent_rm_guard.py -v`
Expected: all 4 tests FAIL — `test_talent_user_cannot_be_granted_group_access` fails because the endpoint currently returns 200 (no guard exists yet); the others fail with a `TypeError` once you add the new `_set_group_access` signature in Step 3 before updating callers, or pass trivially before that change. Confirm the first test specifically returns 200 instead of the expected 400 on the current code.

- [ ] **Step 3: Add `is_talent_role` to `provisioning.py`**

In `backend/app/core/provisioning.py`, after the `TALENT_ROLE_NAME = "Talent"` line (line 12), add:

```python
def is_talent_role(role: Role | None) -> bool:
    """True when `role` is the auto-provisioned, non-editable Talent role."""
    return bool(role and role.is_system and role.name == TALENT_ROLE_NAME)
```

(`Role` is already imported at the top of this file via `from app.db.models import Device, Employee, Role, RolePermission`.)

- [ ] **Step 4: Guard `_set_group_access` in `users.py`**

In `backend/app/api/v1/endpoints/users.py`, add the import (alongside the existing imports near the top):

```python
from app.core.provisioning import is_talent_role
```

Replace the `_set_group_access` function (currently):

```python
async def _set_group_access(emp_id, company_id, group_ids: list[str], db: AsyncSession) -> None:
    await db.execute(delete(EmployeeGroupAccess).where(EmployeeGroupAccess.employee_id == emp_id))
    guids = []
    for gid in dict.fromkeys(group_ids):  # dedupe, preserve order
        try:
            guids.append(uuid.UUID(gid))
        except ValueError:
            continue
    if not guids:
        return
    # Validate every id against the company in one query (was one per group).
    valid = (await db.execute(
        select(Group.id).where(Group.id.in_(guids), Group.company_id == company_id)
    )).scalars().all()
    for guid in valid:
        db.add(EmployeeGroupAccess(employee_id=emp_id, group_id=guid))
```

with:

```python
async def _set_group_access(
    emp_id, company_id, group_ids: list[str], role: Role | None, db: AsyncSession,
) -> None:
    """Replace an employee's group-view (Relation Manager) access.

    `role` must be the employee's role AFTER any change made earlier in the same
    request — every call site already has this in hand, so a role change and a
    group assignment in one request are checked against the same, final role.
    Only a groups-scoped role (e.g. Viewer) may hold group access; Talent users
    are explicitly rejected so they can never become viewers of their own group.
    """
    await db.execute(delete(EmployeeGroupAccess).where(EmployeeGroupAccess.employee_id == emp_id))
    guids = []
    for gid in dict.fromkeys(group_ids):  # dedupe, preserve order
        try:
            guids.append(uuid.UUID(gid))
        except ValueError:
            continue
    if not guids:
        return
    if is_talent_role(role):
        raise HTTPException(
            status_code=400,
            detail="Talent users can't be assigned as Relation Managers. Change the user's role first.",
        )
    if not role or role.scope_type != "groups":
        raise HTTPException(
            status_code=400,
            detail="Only users with a group-scoped role can be assigned group access.",
        )
    # Validate every id against the company in one query (was one per group).
    valid = (await db.execute(
        select(Group.id).where(Group.id.in_(guids), Group.company_id == company_id)
    )).scalars().all()
    for guid in valid:
        db.add(EmployeeGroupAccess(employee_id=emp_id, group_id=guid))
```

- [ ] **Step 5: Update the four call sites to pass the resolved role**

In `create_user`, change:

```python
    await _set_group_access(emp.id, user.company_id, body.group_ids, db)
```

to:

```python
    await _set_group_access(emp.id, user.company_id, body.group_ids, role, db)
```

(`role` is already in scope from `role = await _resolve_role(body.role_id, user.company_id, db)` earlier in the function.)

In `update_user`, replace the whole function body with:

```python
@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserPatch,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    emp = await _get_user(user_id, user.company_id, db)
    changed: dict = {}
    final_role = emp.role_obj
    if body.name is not None and body.name != emp.name:
        changed["name"] = {"from": emp.name, "to": body.name}
        emp.name = body.name
    if body.role_id is not None:
        role = await _resolve_role(body.role_id, user.company_id, db)
        new_role_id = role.id if role else None
        if new_role_id != emp.role_id:
            changed["role"] = role.name if role else None
        emp.role_id = new_role_id
        final_role = role
    if body.password:
        changed["password"] = "changed"
        emp.password_hash = await hash_password_async(body.password)
    if body.is_active is not None and body.is_active != emp.is_active:
        changed["is_active"] = body.is_active
        emp.is_active = body.is_active
    if body.group_ids is not None:
        changed["group_ids"] = body.group_ids
        await _set_group_access(emp.id, user.company_id, body.group_ids, final_role, db)
    if changed:
        await audit.record(db, user, "user.update", "user", user_id,
                           {"email": emp.email, "changes": changed})
    await db.commit()

    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)
```

In `set_user_groups`, change:

```python
    await _set_group_access(emp.id, user.company_id, group_ids, db)
```

to:

```python
    await _set_group_access(emp.id, user.company_id, group_ids, emp.role_obj, db)
```

(`emp` comes from `_get_user`, which already `selectinload`s `role_obj` — safe to read synchronously.)

In `approve_user`, change:

```python
    await _set_group_access(emp.id, user.company_id, body.group_ids, db)
```

to:

```python
    await _set_group_access(emp.id, user.company_id, body.group_ids, role, db)
```

(`role` is already resolved and validated non-null earlier in `approve_user`.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_talent_rm_guard.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 7: Run the full backend suite to check for regressions**

Run: `docker compose run --rm --no-deps backend python -m pytest tests -v`
Expected: PASS (no regressions in `test_provisioning.py`, `test_scoping.py`, `test_device_archive_delete.py`, etc. — none of them call `_set_group_access` with the old signature directly).

- [ ] **Step 8: Verify no frontend change is needed**

In `dashboard/src/app/(dashboard)/users/page.tsx`, `UserModal`'s group-access checkbox tree is already gated by `showGroups = selectedRole?.scope_type === "groups"` (line 35), and the Talent role's `scope_type` is always `"own"` — so the group-access UI is already hidden whenever a Talent role is selected, in both `UserModal` and `ApproveModal`. No frontend edit is required for Feature 1. Confirm by reading `dashboard/src/app/(dashboard)/users/page.tsx:34-35` and `:180-181` and noting both modals compute `showGroups` the same way.

- [ ] **Step 9: Commit**

```bash
git add backend/app/core/provisioning.py backend/app/api/v1/endpoints/users.py backend/tests/test_talent_rm_guard.py
git commit -m "Block Talent users from being assigned Relation Manager group access"
```

---

## Task 2: `user_kind` field on the Users API (Feature 2, backend)

**Files:**
- Modify: `backend/app/schemas/rbac.py:57-68` (`UserOut`)
- Modify: `backend/app/api/v1/endpoints/users.py:39-49` (`_user_out`), `:152-164` (`list_users` return)
- Test: Create `backend/tests/test_user_kind.py`

**Interfaces:**
- Consumes: `is_talent_role` from Task 1 (`app.core.provisioning`).
- Produces: `UserOut.user_kind: "talent" | "team"` — consumed by Task 3 (frontend tabs).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_user_kind.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_user_kind.py -v`
Expected: FAIL with a `KeyError: 'user_kind'` (field does not exist in the response yet).

- [ ] **Step 3: Add `user_kind` to `UserOut`**

In `backend/app/schemas/rbac.py`, replace the `UserOut` class:

```python
class UserOut(BaseModel):
    id: str
    name: str
    email: str
    role_id: Optional[str] = None
    role_name: Optional[str] = None
    scope_type: Optional[str] = None
    is_active: bool
    is_approved: bool = True
    group_ids: list[str] = []
    device_count: int = 0
    created_at: Optional[str] = None
```

with:

```python
class UserOut(BaseModel):
    id: str
    name: str
    email: str
    role_id: Optional[str] = None
    role_name: Optional[str] = None
    scope_type: Optional[str] = None
    user_kind: str = "team"               # "talent" | "team"
    is_active: bool
    is_approved: bool = True
    group_ids: list[str] = []
    device_count: int = 0
    created_at: Optional[str] = None
```

- [ ] **Step 4: Populate `user_kind` in `users.py`**

In `_user_out`, replace:

```python
async def _user_out(emp: Employee, db: AsyncSession) -> UserOut:
    return UserOut(
        id=str(emp.id), name=emp.name, email=emp.email,
        role_id=str(emp.role_id) if emp.role_id else None,
        role_name=emp.role_obj.name if emp.role_obj else (emp.role or None),
        scope_type=emp.role_obj.scope_type if emp.role_obj else None,
        is_active=emp.is_active,
        is_approved=getattr(emp, "is_approved", True),
        group_ids=await _group_ids(emp.id, db),
        device_count=await _device_count(emp.id, db),
    )
```

with:

```python
async def _user_out(emp: Employee, db: AsyncSession) -> UserOut:
    return UserOut(
        id=str(emp.id), name=emp.name, email=emp.email,
        role_id=str(emp.role_id) if emp.role_id else None,
        role_name=emp.role_obj.name if emp.role_obj else (emp.role or None),
        scope_type=emp.role_obj.scope_type if emp.role_obj else None,
        user_kind="talent" if is_talent_role(emp.role_obj) else "team",
        is_active=emp.is_active,
        is_approved=getattr(emp, "is_approved", True),
        group_ids=await _group_ids(emp.id, db),
        device_count=await _device_count(emp.id, db),
    )
```

In `list_users`, replace the return block:

```python
    return [
        UserOut(
            id=str(e.id), name=e.name, email=e.email,
            role_id=str(e.role_id) if e.role_id else None,
            role_name=e.role_obj.name if e.role_obj else (e.role or None),
            scope_type=e.role_obj.scope_type if e.role_obj else None,
            is_active=e.is_active,
            is_approved=getattr(e, "is_approved", True),
            group_ids=groups_by_emp.get(e.id, []),
            device_count=device_counts.get(e.id, 0),
            created_at=e.created_at.isoformat() if e.created_at else None,
        )
        for e in employees
    ]
```

with:

```python
    return [
        UserOut(
            id=str(e.id), name=e.name, email=e.email,
            role_id=str(e.role_id) if e.role_id else None,
            role_name=e.role_obj.name if e.role_obj else (e.role or None),
            scope_type=e.role_obj.scope_type if e.role_obj else None,
            user_kind="talent" if is_talent_role(e.role_obj) else "team",
            is_active=e.is_active,
            is_approved=getattr(e, "is_approved", True),
            group_ids=groups_by_emp.get(e.id, []),
            device_count=device_counts.get(e.id, 0),
            created_at=e.created_at.isoformat() if e.created_at else None,
        )
        for e in employees
    ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_user_kind.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/rbac.py backend/app/api/v1/endpoints/users.py backend/tests/test_user_kind.py
git commit -m "Add user_kind (talent/team) to the users API"
```

---

## Task 3: Users page — Team / Talent / Pending tabs (Feature 2, frontend)

**Files:**
- Modify: `dashboard/src/types/index.ts:103-115` (`User` interface)
- Modify: `dashboard/src/app/(dashboard)/users/page.tsx:236-350` (tabs, filtering, header)

**Interfaces:**
- Consumes: `User.user_kind` from Task 2.
- Produces: no new exports — this is a leaf UI change.

- [ ] **Step 1: Add `user_kind` to the `User` type**

In `dashboard/src/types/index.ts`, replace:

```ts
export interface User {
  id:           string;
  name:         string;
  email:        string;
  role_id:      string | null;
  role_name:    string | null;
  scope_type:   string | null;
  is_active:    boolean;
  is_approved:  boolean;
  group_ids:    string[];
  device_count: number;
  created_at:   string | null;
}
```

with:

```ts
export interface User {
  id:           string;
  name:         string;
  email:        string;
  role_id:      string | null;
  role_name:    string | null;
  scope_type:   string | null;
  user_kind:    "team" | "talent";
  is_active:    boolean;
  is_approved:  boolean;
  group_ids:    string[];
  device_count: number;
  created_at:   string | null;
}
```

- [ ] **Step 2: Update `Tab` type and the initial/derived state**

In `dashboard/src/app/(dashboard)/users/page.tsx`, replace:

```tsx
type Tab = "active" | "pending";

function UsersPageInner() {
  const { loading, can } = useUser();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<Tab>(() =>
    searchParams.get("tab") === "pending" ? "pending" : "active"
  );

  useEffect(() => {
    if (searchParams.get("tab") === "pending") setTab("pending");
  }, [searchParams]);
  const [search,  setSearch]  = useState("");
  const [editing, setEditing] = useState<User | null | "new">(null);
  const [approving, setApproving] = useState<User | null>(null);

  const { data: users,   mutate: mutateUsers,  isLoading: usersLoading }   = useSWR<User[]>("users",         () => getUsers());
  const { data: pending, mutate: mutatePending, isLoading: pendingLoading } = useSWR<User[]>("users-pending", getPendingUsers);
  const { data: roles }  = useSWR<Role[]>("roles-for-users",  getRoles);
  const { data: groups } = useSWR<Group[]>("groups-for-users", getGroups);

  // One batched call for ALL users' live status + time spent; refresh keeps the
  // online dots current without per-user requests.
  const { data: activity } = useSWR<UserActivity[]>("users-activity", getUserActivity, {
    refreshInterval: 30_000,
  });
  const actById = useMemo(
    () => new Map((activity ?? []).map(a => [a.id, a])),
    [activity],
  );

  const debouncedSearch = useDebouncedValue(search, 200);
  const filteredActive = useMemo(() => {
    if (!users) return [];
    if (!debouncedSearch.trim()) return users;
    return users.filter(u => smartMatch(debouncedSearch, u.name, u.email, u.role_name));
  }, [users, debouncedSearch]);
```

with:

```tsx
type Tab = "team" | "talent" | "pending";

function UsersPageInner() {
  const { loading, can } = useUser();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<Tab>(() =>
    searchParams.get("tab") === "pending" ? "pending" : "team"
  );

  useEffect(() => {
    if (searchParams.get("tab") === "pending") setTab("pending");
  }, [searchParams]);
  const [search,  setSearch]  = useState("");
  const [editing, setEditing] = useState<User | null | "new">(null);
  const [approving, setApproving] = useState<User | null>(null);

  const { data: users,   mutate: mutateUsers,  isLoading: usersLoading }   = useSWR<User[]>("users",         () => getUsers());
  const { data: pending, mutate: mutatePending, isLoading: pendingLoading } = useSWR<User[]>("users-pending", getPendingUsers);
  const { data: roles }  = useSWR<Role[]>("roles-for-users",  getRoles);
  const { data: groups } = useSWR<Group[]>("groups-for-users", getGroups);

  // One batched call for ALL users' live status + time spent; refresh keeps the
  // online dots current without per-user requests.
  const { data: activity } = useSWR<UserActivity[]>("users-activity", getUserActivity, {
    refreshInterval: 30_000,
  });
  const actById = useMemo(
    () => new Map((activity ?? []).map(a => [a.id, a])),
    [activity],
  );

  const teamUsers   = useMemo(() => (users ?? []).filter(u => u.user_kind !== "talent"), [users]);
  const talentUsers = useMemo(() => (users ?? []).filter(u => u.user_kind === "talent"), [users]);
  const baseList = tab === "talent" ? talentUsers : teamUsers;

  const debouncedSearch = useDebouncedValue(search, 200);
  const filteredActive = useMemo(() => {
    if (!debouncedSearch.trim()) return baseList;
    return baseList.filter(u => smartMatch(debouncedSearch, u.name, u.email, u.role_name));
  }, [baseList, debouncedSearch]);
```

- [ ] **Step 3: Update the header subtitle and "Add user" visibility**

Replace:

```tsx
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Users</h1>
          <p className="text-gray-600 text-sm mt-0.5">{users?.length ?? 0} active</p>
        </div>
        {tab === "active" && can("users", "create") && (
          <button onClick={() => setEditing("new")}
                  className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
            <Plus size={15} /> Add user
          </button>
        )}
      </div>
```

with:

```tsx
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Users</h1>
          <p className="text-gray-600 text-sm mt-0.5">
            {tab === "talent" ? talentUsers.length : teamUsers.length} {tab === "talent" ? "talent" : "team members"}
          </p>
        </div>
        {(tab === "team" || tab === "talent") && can("users", "create") && (
          <button onClick={() => setEditing("new")}
                  className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
            <Plus size={15} /> Add user
          </button>
        )}
      </div>
```

- [ ] **Step 4: Update the tabs bar and search visibility**

Replace:

```tsx
      {/* Tabs + search */}
      <div className="flex items-center gap-4 mb-4">
        <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden">
          <button onClick={() => setTab("active")}
                  className={`px-3 py-1.5 text-sm font-medium transition ${tab === "active" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600"}`}>
            Active
          </button>
          <button onClick={() => setTab("pending")}
                  className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium transition ${tab === "pending" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600"}`}>
            Pending
            {pendingCount > 0 && (
              <span className="bg-amber-100 text-amber-700 text-xs font-semibold px-1.5 py-0.5 rounded-full">{pendingCount}</span>
            )}
          </button>
        </div>

        {tab === "active" && (
          <div className="relative max-w-xs flex-1">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search name or email…"
                   className="w-full pl-9 pr-9 py-2 bg-white border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
            {search && <button onClick={() => setSearch("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600"><X size={14} /></button>}
          </div>
        )}
      </div>
```

with:

```tsx
      {/* Tabs + search */}
      <div className="flex items-center gap-4 mb-4">
        <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden">
          <button onClick={() => setTab("team")}
                  className={`px-3 py-1.5 text-sm font-medium transition ${tab === "team" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600"}`}>
            Team
          </button>
          <button onClick={() => setTab("talent")}
                  className={`px-3 py-1.5 text-sm font-medium transition ${tab === "talent" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600"}`}>
            Talent
          </button>
          <button onClick={() => setTab("pending")}
                  className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium transition ${tab === "pending" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600"}`}>
            Pending
            {pendingCount > 0 && (
              <span className="bg-amber-100 text-amber-700 text-xs font-semibold px-1.5 py-0.5 rounded-full">{pendingCount}</span>
            )}
          </button>
        </div>

        {(tab === "team" || tab === "talent") && (
          <div className="relative max-w-xs flex-1">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search name or email…"
                   className="w-full pl-9 pr-9 py-2 bg-white border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
            {search && <button onClick={() => setSearch("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600"><X size={14} /></button>}
          </div>
        )}
      </div>
```

- [ ] **Step 5: Update the table's render condition**

Replace the line `{tab === "active" && (` (which opens the active-users table block) with `{(tab === "team" || tab === "talent") && (`. Leave everything inside that block (the table using `filteredActive`, the loading skeleton, and the "No users found" empty state) unchanged — `filteredActive` now already reflects the current tab via Step 2's `baseList`.

- [ ] **Step 6: Verify with a build**

Run: `cd dashboard && npm run build`
Expected: builds successfully with no TypeScript errors.

- [ ] **Step 7: Manual smoke check**

Run: `cd dashboard && npm run dev`, open `/users`. Confirm: three tabs "Team", "Talent", "Pending" render; switching tabs filters the table; search filters within the active tab; a Talent user's row shows the "Talent" role pill; "Add user" appears on Team and Talent tabs but not Pending.

- [ ] **Step 8: Commit**

```bash
git add dashboard/src/types/index.ts "dashboard/src/app/(dashboard)/users/page.tsx"
git commit -m "Split Users page into Team, Talent, and Pending tabs"
```

---

## Task 4: `devices_for_groups_detailed` helper (Feature 3 prep, backend)

Feature 3's notification body needs, for a given offline device, the group it belongs to (for display). `group_tree.py` already has `devices_for_groups` (device ids only); this task adds a detailed variant that also returns the containing group id, and rewrites the plain version to reuse it (avoids duplicating the same SQL).

**Files:**
- Modify: `backend/app/core/group_tree.py:1-61`
- Modify: `backend/tests/test_group_tree.py:1-9` (imports)

**Interfaces:**
- Consumes: nothing new.
- Produces: `devices_for_groups_detailed(group_ids: set[uuid.UUID], db) -> dict[uuid.UUID, uuid.UUID]` (device_id → containing group_id), consumed by Task 5. `devices_for_groups` keeps its existing signature/behavior for its two existing callers (`tech_utilization.py`, `home.py`).

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_group_tree.py`, change the top imports from:

```python
"""Unit tests for nested group helpers."""
import uuid

import pytest

from app.core.group_tree import build_paths, would_create_cycle
from app.db.base import AsyncSessionLocal
from app.db.models import Company, Group
```

to:

```python
"""Unit tests for nested group helpers."""
import uuid

import pytest

from app.core.group_tree import build_paths, devices_for_groups, devices_for_groups_detailed, would_create_cycle
from app.db.base import AsyncSessionLocal
from app.db.models import Company, Device, Employee, Group, GroupMember
```

Then append this test to the end of the file:

```python
@pytest.mark.asyncio
async def test_devices_for_groups_detailed_maps_device_to_group():
    async with AsyncSessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"DevMap-{tag}", enrollment_token=f"tok-{tag}")
        db.add(company)
        await db.flush()

        group = Group(company_id=company.id, name=f"G-{tag}")
        db.add(group)
        await db.flush()

        emp = Employee(company_id=company.id, email=f"e-{tag}@x.io", name="Emp")
        db.add(emp)
        await db.flush()

        direct_device = Device(company_id=company.id, hostname=f"direct-{tag}", platform="macos")
        emp_device = Device(company_id=company.id, hostname=f"emp-{tag}", platform="macos", employee_id=emp.id)
        db.add_all([direct_device, emp_device])
        await db.flush()

        db.add_all([
            GroupMember(group_id=group.id, member_type="device", device_id=direct_device.id),
            GroupMember(group_id=group.id, member_type="employee", employee_id=emp.id),
        ])
        await db.flush()

        detailed = await devices_for_groups_detailed({group.id}, db)
        assert detailed[direct_device.id] == group.id
        assert detailed[emp_device.id] == group.id

        plain = await devices_for_groups({group.id}, db)
        assert set(plain) == {direct_device.id, emp_device.id}

        await db.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_group_tree.py -v`
Expected: FAIL with `ImportError: cannot import name 'devices_for_groups_detailed'`.

- [ ] **Step 3: Implement `devices_for_groups_detailed` and rewrite `devices_for_groups`**

In `backend/app/core/group_tree.py`, replace:

```python
async def devices_for_groups(
    group_ids: set[uuid.UUID],
    db: AsyncSession,
) -> list[uuid.UUID]:
    """Distinct device ids from direct members of the given groups."""
    if not group_ids:
        return []
    rows = await db.execute(
        text(
            """
            SELECT DISTINCT COALESCE(gm.device_id, d.id) AS did
            FROM group_members gm
            LEFT JOIN devices d ON d.employee_id = gm.employee_id
                AND gm.member_type = 'employee'
            WHERE gm.group_id = ANY(:gids)
              AND COALESCE(gm.device_id, d.id) IS NOT NULL
            """
        ),
        {"gids": [str(i) for i in group_ids]},
    )
    return [r[0] for r in rows.all()]
```

with:

```python
async def devices_for_groups_detailed(
    group_ids: set[uuid.UUID],
    db: AsyncSession,
) -> dict[uuid.UUID, uuid.UUID]:
    """Map each device id (direct device member, or device of a member employee)
    within the given groups to one containing group_id. When a device is reachable
    through more than one group in the set, an arbitrary one is kept — good enough
    for display purposes (e.g. notification group-path labels)."""
    if not group_ids:
        return {}
    rows = await db.execute(
        text(
            """
            SELECT gm.group_id, COALESCE(gm.device_id, d.id) AS did
            FROM group_members gm
            LEFT JOIN devices d ON d.employee_id = gm.employee_id
                AND gm.member_type = 'employee'
            WHERE gm.group_id = ANY(:gids)
              AND COALESCE(gm.device_id, d.id) IS NOT NULL
            """
        ),
        {"gids": [str(i) for i in group_ids]},
    )
    out: dict[uuid.UUID, uuid.UUID] = {}
    for gid, did in rows.all():
        out.setdefault(did, gid)
    return out


async def devices_for_groups(
    group_ids: set[uuid.UUID],
    db: AsyncSession,
) -> list[uuid.UUID]:
    """Distinct device ids from direct members of the given groups."""
    return list((await devices_for_groups_detailed(group_ids, db)).keys())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_group_tree.py -v`
Expected: PASS (including the pre-existing `test_build_paths` and `test_would_create_cycle`).

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `docker compose run --rm --no-deps backend python -m pytest tests -v`
Expected: PASS — `tech_utilization.py` and `home.py` call `devices_for_groups` with the same signature and get the same list of device ids as before (order may differ, which those callers don't depend on — they only check set membership).

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/group_tree.py backend/tests/test_group_tree.py
git commit -m "Add devices_for_groups_detailed for group-path lookups"
```

---

## Task 5: Talent-offline notifications feed (Feature 3, backend)

**Files:**
- Modify: `backend/app/api/v1/endpoints/company.py:16-36` (`DEFAULT_SETTINGS`, `_BOOL_KEYS`)
- Modify: `backend/app/api/v1/endpoints/notifications.py` (full rewrite)
- Test: Create `backend/tests/test_talent_offline_notifications.py`

**Interfaces:**
- Consumes: `is_talent_role` (Task 1), `devices_for_groups_detailed` (Task 4), `visible_device_ids`/`ALL` (`app.core.scoping`, pre-existing), `build_paths`/`descendant_group_ids` (`app.core.group_tree`, pre-existing), `DEFAULT_SETTINGS` (`app.api.v1.endpoints.company`).
- Produces: `GET /v1/notifications` now also returns `type: "talent_offline"` entries; `offline_alert_enabled` becomes a valid `companies.settings` key.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_talent_offline_notifications.py`:

```python
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
    company, _, _, _, rm_in, rm_out, _ = await _setup_company_with_offline_talent(db_session, tag)

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_talent_offline_notifications.py -v`
Expected: all 4 FAIL (no `talent_offline` notifications are emitted yet).

- [ ] **Step 3: Update `DEFAULT_SETTINGS` in `company.py`**

In `backend/app/api/v1/endpoints/company.py`, replace:

```python
DEFAULT_SETTINGS: dict = {
    # General
    "timezone":            "UTC",
    "work_start":          "09:00",
    "work_end":            "18:00",
    # Tracking (served to agents via /v1/agent/config)
    "track_urls":          True,
    "track_window_titles": True,
    "track_keyboard":      False,
    "idle_timeout_mins":   5,
    # Notifications
    "email_alerts":        True,
    "idle_alert_mins":     30,
    "offline_alert_mins":  15,
    "daily_report":        True,
    # Shared files — max upload size in MB (see endpoints/adminFiles.py)
    "max_file_size_mb":    500,
}

_BOOL_KEYS = {"track_urls", "track_window_titles", "track_keyboard",
              "email_alerts", "daily_report"}
```

with:

```python
DEFAULT_SETTINGS: dict = {
    # General
    "timezone":            "UTC",
    "work_start":          "09:00",
    "work_end":            "18:00",
    # Tracking (served to agents via /v1/agent/config)
    "track_urls":          True,
    "track_window_titles": True,
    "track_keyboard":      False,
    "idle_timeout_mins":   5,
    # Notifications
    "email_alerts":          True,
    "idle_alert_mins":       30,
    "offline_alert_enabled": True,
    "offline_alert_mins":    30,
    "daily_report":          True,
    # Shared files — max upload size in MB (see endpoints/adminFiles.py)
    "max_file_size_mb":    500,
}

_BOOL_KEYS = {"track_urls", "track_window_titles", "track_keyboard",
              "email_alerts", "daily_report", "offline_alert_enabled"}
```

(`offline_alert_mins` stays in `_INT_KEYS` unchanged — it's already there with range `(0, 1440)`.)

- [ ] **Step 4: Rewrite `notifications.py`**

Replace the entire contents of `backend/app/api/v1/endpoints/notifications.py` with:

```python
"""Notification feed for dashboard users.

Emits two kinds of live-computed notifications — nothing is persisted except
read receipts, so both are recomputed fresh on every poll:
  - approval_request : pending user registrations, for anyone with users.edit.
  - talent_offline    : talent devices offline past the company's configured
                        threshold, for Super Admins (company-wide) and Relation
                        Managers (limited to their assigned groups + subgroups).
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db, get_current_user
from app.api.v1.endpoints.company import DEFAULT_SETTINGS
from app.core.group_tree import build_paths, descendant_group_ids, devices_for_groups_detailed
from app.core.provisioning import is_talent_role
from app.core.scoping import ALL, visible_device_ids
from app.db.models import Device, Employee, EmployeeGroupAccess, Group, NotificationRead

router = APIRouter()


def _can_manage_users(user: Employee) -> bool:
    scope = getattr(user, "_scope_type", "own")
    if scope == "all":
        return True
    perms: set = getattr(user, "_perms", set())
    return ("users", "edit") in perms


async def _offline_alert_settings(company_id: uuid.UUID, db: AsyncSession) -> tuple[bool, int]:
    """(enabled, threshold_minutes) — degrades gracefully pre-migration-031."""
    try:
        stored = (await db.execute(
            text("SELECT settings FROM companies WHERE id = :id"),
            {"id": str(company_id)},
        )).scalar() or {}
    except ProgrammingError:
        await db.rollback()
        stored = {}
    merged = {**DEFAULT_SETTINGS, **stored}
    return bool(merged.get("offline_alert_enabled", True)), int(merged.get("offline_alert_mins", 30))


async def _talent_offline_notifications(
    user: Employee, read_keys: set[str], db: AsyncSession,
) -> list[dict]:
    scope = getattr(user, "_scope_type", "own")
    if scope not in ("all", "groups"):
        return []

    enabled, threshold_mins = await _offline_alert_settings(user.company_id, db)
    if not enabled or threshold_mins <= 0:
        return []

    visible = await visible_device_ids(user, db)
    if visible is not ALL and not visible:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_mins)
    q = (
        select(Device, Employee)
        .join(Employee, Employee.id == Device.employee_id)
        .options(selectinload(Employee.role_obj))
        .where(
            Device.company_id == user.company_id,
            Device.is_active == True,      # noqa: E712
            Device.is_archived == False,   # noqa: E712
            Device.is_deleted == False,    # noqa: E712
            Device.last_seen.isnot(None),
            Device.last_seen < cutoff,
        )
    )
    if visible is not ALL:
        q = q.where(Device.id.in_(visible))
    rows = (await db.execute(q)).all()
    candidates = [(d, e) for d, e in rows if is_talent_role(e.role_obj)]
    if not candidates:
        return []

    if scope == "groups":
        ega_rows = await db.execute(
            select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == user.id)
        )
        path_group_ids = await descendant_group_ids([r[0] for r in ega_rows.all()], user.company_id, db)
    else:
        gid_rows = await db.execute(
            select(Group.id).where(Group.company_id == user.company_id, Group.is_deleted == False)  # noqa: E712
        )
        path_group_ids = {r[0] for r in gid_rows.all()}

    device_group_map = await devices_for_groups_detailed(path_group_ids, db)
    path_rows = await db.execute(
        select(Group.id, Group.name, Group.parent_id).where(
            Group.id.in_(path_group_ids), Group.company_id == user.company_id, Group.is_deleted == False,  # noqa: E712
        )
    )
    paths = build_paths([(gid, name, pid) for gid, name, pid in path_rows.all()])

    notifications: list[dict] = []
    for device, emp in candidates:
        minutes = int((datetime.now(timezone.utc) - device.last_seen).total_seconds() // 60)
        key = f"talent_offline:{device.id}:{device.last_seen.isoformat()}"
        group_id = device_group_map.get(device.id)
        path = paths.get(group_id) if group_id else None
        body_parts = [device.hostname, f"offline for {minutes} min"]
        if path:
            body_parts.append(path)
        body_parts.append(f"last seen {device.last_seen.strftime('%b %d, %I:%M %p UTC')}")
        notifications.append({
            "key": key,
            "type": "talent_offline",
            "title": f"⚠ {emp.name}'s device is offline",
            "body": " · ".join(body_parts),
            "created_at": device.last_seen.isoformat(),
            "is_read": key in read_keys,
            "metadata": {"device_id": str(device.id)},
        })
    return notifications


@router.get("")
async def list_notifications(
    user: Employee = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notifications: list[dict] = []

    read_result = await db.execute(
        select(NotificationRead.notification_key).where(
            NotificationRead.employee_id == user.id
        )
    )
    read_keys: set[str] = {r[0] for r in read_result.all()}

    if _can_manage_users(user):
        pending_result = await db.execute(
            select(Employee)
            .where(
                Employee.company_id == user.company_id,
                Employee.is_approved == False,   # noqa: E712
                Employee.is_active == True,       # noqa: E712
                Employee.is_deleted == False,     # noqa: E712
            )
            .order_by(Employee.created_at.desc())
        )
        pending = pending_result.scalars().all()

        for p in pending:
            key = f"approval:{p.id}"
            notifications.append({
                "key": key,
                "type": "approval_request",
                "title": f"{p.name} requested access",
                "body": p.email,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "is_read": key in read_keys,
                "metadata": {"user_id": str(p.id)},
            })

    notifications.extend(await _talent_offline_notifications(user, read_keys, db))

    return notifications


class MarkReadBody(BaseModel):
    keys: list[str]


@router.post("/mark-read", status_code=204)
async def mark_read(
    body: MarkReadBody,
    user: Employee = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not body.keys:
        return

    # Single multi-row insert; dedupe so the same key isn't sent twice.
    await db.execute(
        pg_insert(NotificationRead)
        .values([
            {"employee_id": user.id, "notification_key": key}
            for key in dict.fromkeys(body.keys)
        ])
        .on_conflict_do_nothing()
    )
    await db.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_talent_offline_notifications.py -v`
Expected: all 4 PASS.

- [ ] **Step 6: Run the full backend suite to check for regressions**

Run: `docker compose run --rm --no-deps backend python -m pytest tests -v`
Expected: PASS — the existing `approval_request` behavior is unchanged (same query, same key format); confirm no test asserts the old `offline_alert_mins` default of 15.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/endpoints/company.py backend/app/api/v1/endpoints/notifications.py backend/tests/test_talent_offline_notifications.py
git commit -m "Add offline talent-device alerts to the notification feed"
```

---

## Task 6: Settings UI + NotificationBell rendering (Feature 3, frontend)

**Files:**
- Modify: `dashboard/src/lib/api.ts:537-550` (`CompanySettings`)
- Modify: `dashboard/src/app/(dashboard)/settings/page.tsx:27-33` (`DEFAULT_SETTINGS`), `:71-84` (`SelectInput`), `:483-498` (Notifications tab)
- Modify: `dashboard/src/components/NotificationBell.tsx`

**Interfaces:**
- Consumes: `offline_alert_enabled`/`offline_alert_mins` from `GET /v1/company/info` (Task 5); `Notification.metadata.device_id` (pre-existing type, `dashboard/src/types/index.ts:276-284`).
- Produces: no new exports — leaf UI change.

- [ ] **Step 1: Add `offline_alert_enabled` to the `CompanySettings` type**

In `dashboard/src/lib/api.ts`, replace:

```ts
export interface CompanySettings {
  timezone: string;
  work_start: string;
  work_end: string;
  track_urls: boolean;
  track_window_titles: boolean;
  track_keyboard: boolean;
  idle_timeout_mins: number;
  email_alerts: boolean;
  idle_alert_mins: number;
  offline_alert_mins: number;
  daily_report: boolean;
  max_file_size_mb: number;
}
```

with:

```ts
export interface CompanySettings {
  timezone: string;
  work_start: string;
  work_end: string;
  track_urls: boolean;
  track_window_titles: boolean;
  track_keyboard: boolean;
  idle_timeout_mins: number;
  email_alerts: boolean;
  idle_alert_mins: number;
  offline_alert_enabled: boolean;
  offline_alert_mins: number;
  daily_report: boolean;
  max_file_size_mb: number;
}
```

- [ ] **Step 2: Update `DEFAULT_SETTINGS` and `SelectInput` in `settings/page.tsx`**

Replace:

```tsx
const DEFAULT_SETTINGS: CompanySettings = {
  timezone: "UTC", work_start: "09:00", work_end: "18:00",
  track_urls: true, track_window_titles: true, track_keyboard: false,
  idle_timeout_mins: 5,
  email_alerts: true, idle_alert_mins: 30, offline_alert_mins: 15, daily_report: true,
  max_file_size_mb: 500,
};
```

with:

```tsx
const DEFAULT_SETTINGS: CompanySettings = {
  timezone: "UTC", work_start: "09:00", work_end: "18:00",
  track_urls: true, track_window_titles: true, track_keyboard: false,
  idle_timeout_mins: 5,
  email_alerts: true, idle_alert_mins: 30,
  offline_alert_enabled: true, offline_alert_mins: 30, daily_report: true,
  max_file_size_mb: 500,
};
```

Replace:

```tsx
function SelectInput({ value, onChange, options }: {
  value: string; onChange: (v: string) => void;
  options: { label: string; value: string }[];
}) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
    >
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}
```

with:

```tsx
function SelectInput({ value, onChange, options, disabled }: {
  value: string; onChange: (v: string) => void;
  options: { label: string; value: string }[]; disabled?: boolean;
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={e => onChange(e.target.value)}
      className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}
```

- [ ] **Step 3: Add the toggle + wire the threshold's disabled state**

Replace:

```tsx
              <SettingRow
                label="Offline alert threshold"
                description="Send an alert when a device goes offline for this duration"
              >
                <SelectInput
                  value={String(prefs.offline_alert_mins)}
                  onChange={v => setPref("offline_alert_mins", parseInt(v) || 0)}
                  options={[
                    { label: "5 minutes", value: "5" },
                    { label: "15 minutes", value: "15" },
                    { label: "30 minutes", value: "30" },
                    { label: "1 hour", value: "60" },
                    { label: "Never", value: "0" },
                  ]}
                />
              </SettingRow>
```

with:

```tsx
              <SettingRow
                label="Alert managers when a talent device goes offline"
                description="Notifies Relation Managers (for their groups) and Super Admins in-app when a talent's device crosses the threshold below."
              >
                <Toggle checked={prefs.offline_alert_enabled} onChange={v => setPref("offline_alert_enabled", v)} disabled={!canEdit} />
              </SettingRow>

              <SettingRow
                label="Offline alert threshold"
                description="How long a talent device must be offline before managers are alerted"
              >
                <SelectInput
                  value={String(prefs.offline_alert_mins)}
                  onChange={v => setPref("offline_alert_mins", parseInt(v) || 0)}
                  disabled={!canEdit || !prefs.offline_alert_enabled}
                  options={[
                    { label: "5 minutes", value: "5" },
                    { label: "15 minutes", value: "15" },
                    { label: "30 minutes", value: "30" },
                    { label: "1 hour", value: "60" },
                    { label: "2 hours", value: "120" },
                    { label: "Never", value: "0" },
                  ]}
                />
              </SettingRow>
```

- [ ] **Step 4: Render `talent_offline` notifications distinctly in `NotificationBell.tsx`**

Replace the import line:

```tsx
import { Bell, UserCheck, CheckCheck, X, Inbox } from "lucide-react";
```

with:

```tsx
import { Bell, UserCheck, CheckCheck, X, Inbox, WifiOff } from "lucide-react";
```

Replace:

```tsx
const TYPE_CONFIG: Record<string, { color: string; dot: string; href: string }> = {
  approval_request: {
    color: "from-blue-500 to-indigo-600",
    dot:   "bg-brand-500",
    href:  "/users?tab=pending",
  },
};
```

with:

```tsx
const TYPE_CONFIG: Record<string, { color: string; dot: string; href: string }> = {
  approval_request: {
    color: "from-blue-500 to-indigo-600",
    dot:   "bg-brand-500",
    href:  "/users?tab=pending",
  },
  talent_offline: {
    color: "from-amber-500 to-red-500",
    dot:   "bg-red-500",
    href:  "/devices",
  },
};

const TYPE_ICON: Record<string, React.ReactNode> = {
  approval_request: <UserCheck size={20} className="text-white" />,
  talent_offline:    <WifiOff size={20} className="text-white" />,
};
```

Replace:

```tsx
function NotifItem({ n, onRead }: { n: Notification; onRead: () => void }) {
  const cfg = TYPE_CONFIG[n.type] ?? { color: "from-gray-400 to-gray-500", dot: "bg-gray-400", href: "/" };

  return (
    <Link
      href={cfg.href}
      onClick={onRead}
      className={`flex items-start gap-3.5 px-4 py-3.5 hover:bg-gray-50/80 transition-colors border-b border-gray-100/80 last:border-0 group ${!n.is_read ? "bg-brand-50/40" : ""}`}
    >
      {/* Avatar */}
      <div className={`relative shrink-0 w-11 h-11 rounded-full bg-gradient-to-br ${cfg.color} flex items-center justify-center shadow-sm`}>
        <UserCheck size={20} className="text-white" />
        {!n.is_read && (
          <span className={`absolute -top-0.5 -right-0.5 w-3 h-3 ${cfg.dot} rounded-full border-2 border-white`} />
        )}
      </div>
```

with:

```tsx
function NotifItem({ n, onRead }: { n: Notification; onRead: () => void }) {
  const cfg = TYPE_CONFIG[n.type] ?? { color: "from-gray-400 to-gray-500", dot: "bg-gray-400", href: "/" };
  const icon = TYPE_ICON[n.type] ?? <UserCheck size={20} className="text-white" />;
  const href = n.type === "talent_offline" && n.metadata?.device_id
    ? `/devices/${n.metadata.device_id}`
    : cfg.href;

  return (
    <Link
      href={href}
      onClick={onRead}
      className={`flex items-start gap-3.5 px-4 py-3.5 hover:bg-gray-50/80 transition-colors border-b border-gray-100/80 last:border-0 group ${!n.is_read ? "bg-brand-50/40" : ""}`}
    >
      {/* Avatar */}
      <div className={`relative shrink-0 w-11 h-11 rounded-full bg-gradient-to-br ${cfg.color} flex items-center justify-center shadow-sm`}>
        {icon}
        {!n.is_read && (
          <span className={`absolute -top-0.5 -right-0.5 w-3 h-3 ${cfg.dot} rounded-full border-2 border-white`} />
        )}
      </div>
```

- [ ] **Step 5: Verify with a build**

Run: `cd dashboard && npm run build`
Expected: builds successfully with no TypeScript errors.

- [ ] **Step 6: Manual smoke check**

Run: `cd dashboard && npm run dev`, open `/settings` → Notifications tab. Confirm: the new toggle appears above "Offline alert threshold"; turning it off visibly disables the threshold dropdown; Save persists both. Then, in the backend DB, set a talent device's `last_seen` to more than the threshold in the past and confirm the bell (`NotificationBell`) shows a red-tinted alert with a WifiOff icon linking to `/devices/{id}`.

- [ ] **Step 7: Commit**

```bash
git add dashboard/src/lib/api.ts "dashboard/src/app/(dashboard)/settings/page.tsx" dashboard/src/components/NotificationBell.tsx
git commit -m "Add offline-alert toggle to Settings and render it in the notification bell"
```

---

## Task 7: `include_subgroups` on group members (Feature 4, backend)

**Files:**
- Modify: `backend/app/schemas/rbac.py:127-132` (`MemberOut`)
- Modify: `backend/app/api/v1/endpoints/groups.py:203-237` (`_members_with_labels`), `:269-304` (`get_group`), `:387-394` (`list_members`)
- Test: Create `backend/tests/test_group_members_subgroups.py`

**Interfaces:**
- Consumes: `descendant_group_ids`, `build_paths` (`app.core.group_tree`, both already imported in `groups.py`).
- Produces: `GET /v1/groups/{id}` and `GET /v1/groups/{id}/members` accept `?include_subgroups=true`; `MemberOut` gains `group_id`, `group_name`, `path`, `direct` (all `None` when `include_subgroups` is not set, preserving today's response shape for existing callers).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_group_members_subgroups.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_group_members_subgroups.py -v`
Expected: `test_include_subgroups_returns_direct_and_inherited_members` fails (`include_subgroups=true` returns the same direct-only set today, and `MemberOut` has no `direct`/`group_name`/`path` keys). `test_member_count_reflects_direct_members_only_even_with_subgroups` currently passes by coincidence (there are zero direct members and `len(members)` also happens to differ) — re-check after Step 3 confirms the fix is exercised; if it already passes, that's fine, the assertion still guards the fixed behavior going forward.

- [ ] **Step 3: Add fields to `MemberOut`**

In `backend/app/schemas/rbac.py`, replace:

```python
class MemberOut(BaseModel):
    id: str
    member_type: str
    employee_id: Optional[str] = None
    device_id: Optional[str] = None
    label: str                             # employee name or device hostname
```

with:

```python
class MemberOut(BaseModel):
    id: str
    member_type: str
    employee_id: Optional[str] = None
    device_id: Optional[str] = None
    label: str                             # employee name or device hostname
    group_id: Optional[str] = None         # populated only when include_subgroups=true
    group_name: Optional[str] = None
    path: Optional[str] = None
    direct: Optional[bool] = None
```

- [ ] **Step 4: Update `_members_with_labels` in `groups.py`**

Replace:

```python
async def _members_with_labels(group_id, db: AsyncSession) -> list[MemberOut]:
    rows = await db.execute(
        select(GroupMember).where(GroupMember.group_id == group_id).order_by(GroupMember.added_at.asc())
    )
    members = rows.scalars().all()
    if not members:
        return []

    emp_ids = [m.employee_id for m in members if m.member_type == "employee" and m.employee_id]
    dev_ids = [m.device_id   for m in members if m.member_type == "device"   and m.device_id]

    emp_names: dict = {}
    if emp_ids:
        er = await db.execute(select(Employee.id, Employee.name).where(Employee.id.in_(emp_ids)))
        emp_names = {eid: name for eid, name in er.all()}
    dev_names: dict = {}
    if dev_ids:
        dr = await db.execute(select(Device.id, Device.hostname).where(Device.id.in_(dev_ids)))
        dev_names = {did: host for did, host in dr.all()}

    out: list[MemberOut] = []
    for m in members:
        if m.member_type == "employee" and m.employee_id:
            label = emp_names.get(m.employee_id) or "(unknown user)"
        elif m.member_type == "device" and m.device_id:
            label = dev_names.get(m.device_id) or "(unknown device)"
        else:
            label = ""
        out.append(MemberOut(
            id=str(m.id), member_type=m.member_type,
            employee_id=str(m.employee_id) if m.employee_id else None,
            device_id=str(m.device_id) if m.device_id else None,
            label=label,
        ))
    return out
```

with:

```python
async def _members_with_labels(
    root_group_id, company_id, db: AsyncSession, include_subgroups: bool = False,
) -> list[MemberOut]:
    gids = await descendant_group_ids([root_group_id], company_id, db) if include_subgroups else {root_group_id}

    rows = await db.execute(
        select(GroupMember).where(GroupMember.group_id.in_(gids)).order_by(GroupMember.added_at.asc())
    )
    members = rows.scalars().all()
    if not members:
        return []

    emp_ids = [m.employee_id for m in members if m.member_type == "employee" and m.employee_id]
    dev_ids = [m.device_id   for m in members if m.member_type == "device"   and m.device_id]

    emp_names: dict = {}
    if emp_ids:
        er = await db.execute(select(Employee.id, Employee.name).where(Employee.id.in_(emp_ids)))
        emp_names = {eid: name for eid, name in er.all()}
    dev_names: dict = {}
    if dev_ids:
        dr = await db.execute(select(Device.id, Device.hostname).where(Device.id.in_(dev_ids)))
        dev_names = {did: host for did, host in dr.all()}

    group_names: dict = {}
    paths: dict = {}
    if include_subgroups:
        grp_rows = await db.execute(select(Group.id, Group.name, Group.parent_id).where(Group.id.in_(gids)))
        grp_list = [(gid, name, pid) for gid, name, pid in grp_rows.all()]
        group_names = {gid: name for gid, name, _ in grp_list}
        paths = build_paths(grp_list)

    out: list[MemberOut] = []
    for m in members:
        if m.member_type == "employee" and m.employee_id:
            label = emp_names.get(m.employee_id) or "(unknown user)"
        elif m.member_type == "device" and m.device_id:
            label = dev_names.get(m.device_id) or "(unknown device)"
        else:
            label = ""
        out.append(MemberOut(
            id=str(m.id), member_type=m.member_type,
            employee_id=str(m.employee_id) if m.employee_id else None,
            device_id=str(m.device_id) if m.device_id else None,
            label=label,
            group_id=str(m.group_id) if include_subgroups else None,
            group_name=group_names.get(m.group_id) if include_subgroups else None,
            path=paths.get(m.group_id) if include_subgroups else None,
            direct=(m.group_id == root_group_id) if include_subgroups else None,
        ))
    return out
```

- [ ] **Step 5: Update `get_group` and `list_members` to accept and pass `include_subgroups`**

Replace:

```python
@router.get("/{group_id}", response_model=GroupDetailOut)
async def get_group(
    group_id: str,
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    members = await _members_with_labels(g.id, db)
```

with:

```python
@router.get("/{group_id}", response_model=GroupDetailOut)
async def get_group(
    group_id: str,
    include_subgroups: bool = Query(default=False),
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    members = await _members_with_labels(g.id, user.company_id, db, include_subgroups=include_subgroups)
```

Further down in the same function, replace:

```python
        member_count=mc_map.get(g.id, len(members)),
```

with:

```python
        member_count=mc_map.get(g.id, 0),
```

(This is a pre-existing latent bug: the fallback used `len(members)`, which was only ever correct because there was no way to fetch more than direct members before this task. `member_count` is a direct-member summary badge, so its fallback must be `0`, not the (now potentially much larger) `len(members)`.)

Replace:

```python
@router.get("/{group_id}/members", response_model=list[MemberOut])
async def list_members(
    group_id: str,
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    return await _members_with_labels(g.id, db)
```

with:

```python
@router.get("/{group_id}/members", response_model=list[MemberOut])
async def list_members(
    group_id: str,
    include_subgroups: bool = Query(default=False),
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    return await _members_with_labels(g.id, user.company_id, db, include_subgroups=include_subgroups)
```

(`build_paths`, `descendant_group_ids`, and `Query` are already imported at the top of `groups.py` — no import changes needed.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_group_members_subgroups.py -v`
Expected: both PASS.

- [ ] **Step 7: Run the full backend suite to check for regressions**

Run: `docker compose run --rm --no-deps backend python -m pytest tests -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/schemas/rbac.py backend/app/api/v1/endpoints/groups.py backend/tests/test_group_members_subgroups.py
git commit -m "Add include_subgroups to group member listing, fix member_count fallback"
```

---

## Task 8: Group detail page — include-subgroups toggle (Feature 4, frontend)

**Files:**
- Modify: `dashboard/src/types/index.ts:180-186` (`GroupMember`)
- Modify: `dashboard/src/lib/api.ts:245` (`getGroup`)
- Modify: `dashboard/src/app/(dashboard)/groups/[id]/page.tsx:376-419` (state/hooks), `:389-392` (`removeMember`), `:555-599` (members panel)

**Interfaces:**
- Consumes: `MemberOut` extra fields and `include_subgroups` query param from Task 7.
- Produces: no new exports — leaf UI change.

- [ ] **Step 1: Add fields to the `GroupMember` type**

In `dashboard/src/types/index.ts`, replace:

```ts
export interface GroupMember {
  id:          string;
  member_type: string;            // 'employee' | 'device'
  employee_id: string | null;
  device_id:   string | null;
  label:       string;
}
```

with:

```ts
export interface GroupMember {
  id:          string;
  member_type: string;            // 'employee' | 'device'
  employee_id: string | null;
  device_id:   string | null;
  label:       string;
  group_id:    string | null;     // populated only when fetched with include_subgroups=true
  group_name:  string | null;
  path:        string | null;
  direct:      boolean | null;
}
```

- [ ] **Step 2: Add the `includeSubgroups` param to `getGroup`**

In `dashboard/src/lib/api.ts`, replace:

```ts
export const getGroup   = (id: string) => request<GroupDetail>(`/v1/groups/${id}`);
```

with:

```ts
export const getGroup = (id: string, includeSubgroups = false) =>
  request<GroupDetail>(`/v1/groups/${id}${includeSubgroups ? "?include_subgroups=true" : ""}`);
```

- [ ] **Step 3: Add state, the SWR key, and the sections `useMemo` — all BEFORE the component's early returns**

In `dashboard/src/app/(dashboard)/groups/[id]/page.tsx`, replace:

```tsx
export default function GroupDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { loading, can } = useUser();
  const { data: group, mutate } = useSWR<GroupDetail>(["group", id], () => getGroup(id));
  const [adding, setAdding] = useState(false);
  const [addMemberGroupId, setAddMemberGroupId] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [showSubgroupModal, setShowSubgroupModal] = useState(false);
  const [busySubgroup, setBusySubgroup] = useState("");

  if (loading) return <PageLoader />;
  if (!can("groups", "view")) return <AccessDenied label="Groups" />;

  async function removeMember(memberId: string) {
    try { await removeGroupMember(id, memberId); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }
```

with:

```tsx
export default function GroupDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { loading, can } = useUser();
  const [includeSubgroups, setIncludeSubgroups] = useState(false);
  const { data: group, mutate } = useSWR<GroupDetail>(
    ["group", id, includeSubgroups], () => getGroup(id, includeSubgroups),
  );
  const [adding, setAdding] = useState(false);
  const [addMemberGroupId, setAddMemberGroupId] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [showSubgroupModal, setShowSubgroupModal] = useState(false);
  const [busySubgroup, setBusySubgroup] = useState("");

  const memberSections = useMemo(() => {
    const members = group?.members ?? [];
    if (!includeSubgroups) return [{ label: "Direct members", members }];
    const map = new Map<string, typeof members>();
    for (const m of members) {
      const label = m.direct ? "Direct members" : (m.path ?? "Other");
      if (!map.has(label)) map.set(label, []);
      map.get(label)!.push(m);
    }
    return Array.from(map.entries())
      .sort(([a], [b]) => (a === "Direct members" ? -1 : b === "Direct members" ? 1 : a.localeCompare(b)))
      .map(([label, sectionMembers]) => ({ label, members: sectionMembers }));
  }, [group?.members, includeSubgroups]);

  if (loading) return <PageLoader />;
  if (!can("groups", "view")) return <AccessDenied label="Groups" />;

  async function removeMember(member: GroupMember) {
    try { await removeGroupMember(member.group_id ?? id, member.id); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }
```

(`useMemo` is already imported at the top of the file. `memberSections` is computed with a hook BEFORE the early returns so the Rules of Hooks are respected — every render calls the same hooks in the same order.)

- [ ] **Step 4: Replace the single members table with the toggle + sectioned rendering**

Replace:

```tsx
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-2.5 bg-gray-50 border-b border-gray-100 text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Direct members
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[600px]">
            <thead>
              <tr className="bg-gray-50/50 text-gray-500 text-xs">
                <th className="text-left px-4 py-2.5 font-medium">Member</th>
                <th className="text-left px-4 py-2.5 font-medium">Type</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {group?.members.map(m => (
                <tr key={m.id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      {m.member_type === "employee" ? <UserIcon size={14} className="text-gray-400" /> : <Monitor size={14} className="text-gray-400" />}
                      {m.member_type === "employee" && m.employee_id ? (
                        <Link href={`/users/${m.employee_id}`} className="font-medium text-gray-800 hover:text-brand-600">{m.label}</Link>
                      ) : m.member_type === "device" && m.device_id ? (
                        <Link href={`/devices/${m.device_id}`} className="font-medium text-gray-800 hover:text-brand-600">{m.label}</Link>
                      ) : (
                        <span className="font-medium text-gray-800">{m.label}</span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 capitalize text-gray-500">{m.member_type}</td>
                  <td className="px-4 py-3 text-right">
                    {can("groups", "edit") && (
                      <button onClick={() => removeMember(m.id)} className="text-gray-400 hover:text-red-600 p-1">
                        <Trash2 size={15} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {group?.members.length === 0 && (
                <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-500 text-sm">No members yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
```

with:

```tsx
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-2.5 bg-gray-50 border-b border-gray-100 flex items-center justify-between">
          <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Members</span>
          {!!group?.child_count && (
            <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={includeSubgroups}
                onChange={e => setIncludeSubgroups(e.target.checked)}
                className="accent-[#4a0f70]"
              />
              Include subgroups
            </label>
          )}
        </div>
        {(group?.members.length ?? 0) === 0 ? (
          <div className="px-4 py-10 text-center text-gray-500 text-sm">No members yet.</div>
        ) : (
          memberSections.map(section => (
            <div key={section.label}>
              <div className="px-4 py-1.5 bg-gray-50/60 text-[11px] font-medium text-gray-500">
                {section.label} <span className="text-gray-400">· {section.members.length}</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm min-w-[600px]">
                  <thead className="sr-only">
                    <tr>
                      <th>Member</th>
                      <th>Type</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {section.members.map(m => (
                      <tr key={m.id} className="border-t border-gray-100 hover:bg-gray-50">
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            {m.member_type === "employee" ? <UserIcon size={14} className="text-gray-400" /> : <Monitor size={14} className="text-gray-400" />}
                            {m.member_type === "employee" && m.employee_id ? (
                              <Link href={`/users/${m.employee_id}`} className="font-medium text-gray-800 hover:text-brand-600">{m.label}</Link>
                            ) : m.member_type === "device" && m.device_id ? (
                              <Link href={`/devices/${m.device_id}`} className="font-medium text-gray-800 hover:text-brand-600">{m.label}</Link>
                            ) : (
                              <span className="font-medium text-gray-800">{m.label}</span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 capitalize text-gray-500">{m.member_type}</td>
                        <td className="px-4 py-3 text-right">
                          {can("groups", "edit") && (
                            <button onClick={() => removeMember(m)} className="text-gray-400 hover:text-red-600 p-1">
                              <Trash2 size={15} />
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))
        )}
      </div>
```

- [ ] **Step 5: Verify with a build**

Run: `cd dashboard && npm run build`
Expected: builds successfully with no TypeScript errors.

- [ ] **Step 6: Manual smoke check**

Run: `cd dashboard && npm run dev`, open a group with at least one subgroup that has its own member. Confirm: "Include subgroups" checkbox only appears when the group has subgroups; toggling it on splits the list into a "Direct members" section and one section per subgroup path, each showing a count; toggling off returns to the original single-table view; removing an inherited member (while toggled on) succeeds and the member disappears after refetch.

- [ ] **Step 7: Commit**

```bash
git add dashboard/src/types/index.ts dashboard/src/lib/api.ts "dashboard/src/app/(dashboard)/groups/[id]/page.tsx"
git commit -m "Show subgroup members on the group detail page via an include-subgroups toggle"
```

---

## Task 9: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the complete backend test suite**

Run: `docker compose run --rm --no-deps backend python -m pytest tests -v`
Expected: all tests PASS, including every new test file from Tasks 1, 2, 4, 5, 7. If anything fails, return to the task that owns the failing area and fix it there — do not patch symptoms in this task.

- [ ] **Step 2: Run the frontend build**

Run: `cd dashboard && npm run build`
Expected: builds successfully with no TypeScript errors.

- [ ] **Step 3: Start the full stack and manually walk all four features once, end to end**

Run: `docker compose up -d` (backend + dashboard + db + redis + minio), then in a browser:
1. Users page: confirm Team/Talent/Pending tabs; confirm you cannot select a Talent-scoped group-access UI for a Talent user (it's absent).
2. Attempt (via `curl` or the API docs at `/docs`) a `PUT /v1/users/{talent_id}/groups` for a real Talent user and confirm `400`.
3. Settings → Notifications: toggle the offline alert, save, reload, confirm it persisted.
4. Groups → open a group with a subgroup, toggle "Include subgroups", confirm sectioned members render.

No code changes are expected in this task — it exists purely to catch integration issues the per-task manual checks might have missed (e.g. a stale SWR cache key, a route that isn't registered). If something surfaces, open a small follow-up fix in the owning task's files rather than expanding this task's scope.
