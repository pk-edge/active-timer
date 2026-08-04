# Group "Manage Members" Permission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admins grant a role the ability to add/remove group members independently of the ability to rename/reparent a group, via a new `groups.manage_members` RBAC permission.

**Architecture:** Add one new action (`manage_members`) to the `groups` module in the RBAC permission catalog. Re-gate the two group-membership endpoints (`POST /groups/{id}/members`, `DELETE /groups/{id}/members/{id}`) on it instead of `groups.edit`. Ship a backfill migration so every role currently holding `groups.edit` keeps its existing ability to add/remove members. Update the two dashboard screens that gate these actions in the UI.

**Tech Stack:** FastAPI + SQLAlchemy (async) + PostgreSQL backend; Next.js/TypeScript dashboard. Backend tests run with `pytest-asyncio` against a live Postgres inside Docker.

## Global Constraints

- No DB schema change — `role_permissions.action` is a free-form `String` column (see `backend/app/db/models.py:145-146`), so no `ALTER TABLE` is needed for the new action value.
- `POST /v1/groups/import` keeps its current `groups.create` gate — out of scope (see spec Non-Goals).
- The backfill migration must be idempotent (`ON CONFLICT ... DO NOTHING`) — migrations run automatically against the shared dev/test Postgres and must be safe to re-run.
- Backend tests run via: `docker compose run --rm --no-deps backend python -m pytest tests/<file> -v` (the compose Postgres/Redis must already be up — see `backend/tests/test_e2eApplication.py:14-15`).
- Frontend has no test runner configured (`dashboard/package.json` has no `test` script) — verify frontend changes with `npm run build` (type-checks, per `dashboard/next.config.js` not disabling TS build errors) and, where practical, manual inspection.

---

### Task 1: Backend — `groups.manage_members` permission, endpoint gating, and backfill migration

**Files:**
- Modify: `backend/app/core/permissions.py:30`
- Modify: `backend/app/api/v1/endpoints/groups.py:415-461` (`add_member`), `:464-488` (`remove_member`)
- Create: `backend/app/db/migrations/032_group_manage_members_permission.sql`
- Test: `backend/tests/test_group_manage_members_permission.py` (new file)

**Interfaces:**
- Consumes: `app.core.permissions.MODULES` (existing catalog list), `app.api.deps.require(module, action)` (existing dependency factory, unchanged signature), `app.db.models.Company/Employee/Group/Role/RolePermission` (existing ORM models), `app.core.security.create_admin_token(user_id: str, role: str, company_id: str) -> str` (existing, unchanged).
- Produces: the string `"manage_members"` becomes a valid `(module, action)` pair for `groups` — later tasks (frontend) reference this exact string.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_group_manage_members_permission.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_group_manage_members_permission.py -v`

Expected: `test_manage_members_permission_allows_add_remove_but_not_edit` FAILs on the `POST .../members` assertion with `403` (because `groups.manage_members` isn't a recognized action yet — `is_valid()` returns `False` for it, but since the endpoint currently requires `groups.edit` and the test role doesn't hold it, you get `403` there too — the important failure is that this happens for the WRONG reason). `test_edit_permission_alone_no_longer_allows_add_member` FAILs on the final assertion (`add_member` still succeeds with `201`, not `403`, because the endpoint still requires only `groups.edit`). `test_backfill_migration_grants_manage_members_to_edit_holders` FAILs with a `FileNotFoundError` (migration file doesn't exist yet).

- [ ] **Step 3: Add the `manage_members` action to the permission catalog**

In `backend/app/core/permissions.py`, change line 30:

```python
    {"key": "groups",     "label": "Groups",   "actions": ["view", "create", "edit", "delete", "manage_members"]},
```

- [ ] **Step 4: Re-gate the add/remove member endpoints**

In `backend/app/api/v1/endpoints/groups.py`, change the `add_member` endpoint's dependency:

```python
@router.post("/{group_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    group_id: str,
    body: MemberIn,
    user: Employee = Depends(require("groups", "manage_members")),
    db: AsyncSession = Depends(get_db),
):
```

And the `remove_member` endpoint's dependency:

```python
@router.delete("/{group_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    group_id: str,
    member_id: str,
    user: Employee = Depends(require("groups", "manage_members")),
    db: AsyncSession = Depends(get_db),
):
```

Every other `require("groups", ...)` call in this file (`create_group`, `update_group`, `delete_group`, `archive_group`, `unarchive_group`, `import_groups`, `list_groups`, `get_group`, `list_members`) stays unchanged.

- [ ] **Step 5: Create the backfill migration**

Create `backend/app/db/migrations/032_group_manage_members_permission.sql`:

```sql
-- Backfill groups.manage_members for every role that already holds groups.edit,
-- so existing roles don't lose the ability to add/remove group members now that
-- add/remove is split into its own permission. Safe to run repeatedly.
INSERT INTO role_permissions (role_id, module, action)
SELECT rp.role_id, 'groups', 'manage_members'
FROM role_permissions rp
WHERE rp.module = 'groups' AND rp.action = 'edit'
ON CONFLICT (role_id, module, action) DO NOTHING;
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_group_manage_members_permission.py -v`

Expected: all 3 tests PASS.

- [ ] **Step 7: Run the full backend suite to check for regressions**

Run: `docker compose run --rm --no-deps backend python -m pytest tests -v`

Expected: all tests PASS (existing group/RBAC tests authenticate as an all-scope admin, which holds every permission dynamically — see `backend/app/api/deps.py:255-256` — so they're unaffected by this change).

- [ ] **Step 8: Commit**

```bash
git add backend/app/core/permissions.py backend/app/api/v1/endpoints/groups.py \
        backend/app/db/migrations/032_group_manage_members_permission.sql \
        backend/tests/test_group_manage_members_permission.py
git commit -m "feat: add groups.manage_members permission for add/remove of group members"
```

---

### Task 2: Frontend — expose `manage_members` in the Roles permission matrix

**Files:**
- Modify: `dashboard/src/app/(dashboard)/roles/page.tsx:13-135`

**Interfaces:**
- Consumes: `ModuleDef` (`dashboard/src/types/index.ts:83`, unchanged — `actions: string[]` already accepts the new value with no type change), `GET /v1/roles/modules` (Task 1 makes it return `manage_members` in the `groups` module's `actions` array).
- Produces: nothing consumed by later tasks — this is a leaf UI change.

- [ ] **Step 1: Add the new column and a label map**

In `dashboard/src/app/(dashboard)/roles/page.tsx`, after the `TALENT_ROLE` constant (line 19), add:

```tsx
const PERMISSION_ACTIONS = ["view", "create", "edit", "delete", "manage_members"] as const;
const ACTION_LABELS: Record<string, string> = { manage_members: "Members" };
```

- [ ] **Step 2: Use the new column list and label map in the table header**

Replace:

```tsx
                <thead>
                  <tr className="bg-gray-50 text-gray-600 text-xs">
                    <th className="text-left px-3 py-2 font-medium">Module</th>
                    {["view", "create", "edit", "delete"].map(a => <th key={a} className="px-2 py-2 font-medium capitalize w-16">{a}</th>)}
                  </tr>
                </thead>
```

with:

```tsx
                <thead>
                  <tr className="bg-gray-50 text-gray-600 text-xs">
                    <th className="text-left px-3 py-2 font-medium">Module</th>
                    {PERMISSION_ACTIONS.map(a => <th key={a} className="px-2 py-2 font-medium capitalize w-16">{ACTION_LABELS[a] ?? a}</th>)}
                  </tr>
                </thead>
```

- [ ] **Step 3: Use the new column list in the table body**

Replace:

```tsx
                <tbody>
                  {modules.map(m => (
                    <tr key={m.key} className="border-t border-gray-100">
                      <td className="px-3 py-2 text-gray-700">{m.label}</td>
                      {["view", "create", "edit", "delete"].map(a => (
                        <td key={a} className="text-center">
                          {m.actions.includes(a) ? (
                            <input type="checkbox" checked={perms.has(`${m.key}:${a}`)} onChange={() => toggle(m.key, a)}
                                   className="w-4 h-4 accent-[#4a0f70]" />
                          ) : <span className="text-gray-200">—</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
```

with:

```tsx
                <tbody>
                  {modules.map(m => (
                    <tr key={m.key} className="border-t border-gray-100">
                      <td className="px-3 py-2 text-gray-700">{m.label}</td>
                      {PERMISSION_ACTIONS.map(a => (
                        <td key={a} className="text-center">
                          {m.actions.includes(a) ? (
                            <input type="checkbox" checked={perms.has(`${m.key}:${a}`)} onChange={() => toggle(m.key, a)}
                                   className="w-4 h-4 accent-[#4a0f70]" />
                          ) : <span className="text-gray-200">—</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
```

- [ ] **Step 4: Type-check and build**

Run: `cd dashboard && npm run build`

Expected: build succeeds with no TypeScript errors.

- [ ] **Step 5: Manually verify in the browser**

Run: `cd dashboard && npm run dev`, log in as an all-scope admin, open **Roles**, click into any non-system role, and confirm the permission matrix now shows a 5th "Members" column for the "Groups" row with a working checkbox, and shows "—" for every other module's row in that column (since only `groups` has this action).

- [ ] **Step 6: Commit**

```bash
git add "dashboard/src/app/(dashboard)/roles/page.tsx"
git commit -m "feat: expose groups.manage_members as a Members column in the role editor"
```

---

### Task 3: Frontend — split `groups.edit` gating on the group detail page

**Files:**
- Modify: `dashboard/src/app/(dashboard)/groups/[id]/page.tsx:465-479` (header buttons), `:504-528` (subgroup row icons), `:621-625` (member row delete)

**Interfaces:**
- Consumes: `useUser().can(module, action)` (existing hook, unchanged signature, `dashboard/src/hooks/useUser.ts`).
- Produces: nothing consumed by later tasks — leaf UI change.

- [ ] **Step 1: Split the header "Edit" and "Add member" buttons**

Replace:

```tsx
        <div className="flex items-center gap-2">
          {can("groups", "edit") && group && (
            <>
              <button onClick={() => setEditing(true)}
                className="flex items-center gap-1.5 px-3 py-2 text-sm border border-gray-200 rounded-lg hover:bg-gray-50 text-gray-700">
                <Pencil size={14} /> Edit
              </button>
              <button onClick={() => setAdding(true)}
                className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
                <UserPlus size={15} /> Add member
              </button>
            </>
          )}
        </div>
```

with:

```tsx
        <div className="flex items-center gap-2">
          {can("groups", "edit") && group && (
            <button onClick={() => setEditing(true)}
              className="flex items-center gap-1.5 px-3 py-2 text-sm border border-gray-200 rounded-lg hover:bg-gray-50 text-gray-700">
              <Pencil size={14} /> Edit
            </button>
          )}
          {can("groups", "manage_members") && group && (
            <button onClick={() => setAdding(true)}
              className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
              <UserPlus size={15} /> Add member
            </button>
          )}
        </div>
```

- [ ] **Step 2: Split the active-subgroup row icon cluster**

Replace:

```tsx
                  {can("groups", "edit") && (
                    <div className="flex items-center gap-0.5 shrink-0">
                      <button
                        onClick={e => { e.preventDefault(); setAddMemberGroupId(c.id); }}
                        disabled={busySubgroup === c.id}
                        title="Add member"
                        className="p-1.5 text-gray-400 hover:text-brand-600 rounded disabled:opacity-40">
                        <UserPlus size={12} />
                      </button>
                      <button
                        onClick={e => { e.preventDefault(); handleArchiveSubgroup(c.id); }}
                        disabled={busySubgroup === c.id}
                        title="Archive"
                        className="p-1.5 text-gray-400 hover:text-amber-600 rounded disabled:opacity-40">
                        <Archive size={12} />
                      </button>
                      <button
                        onClick={e => { e.preventDefault(); handleDeleteSubgroup(c.id, c.name); }}
                        disabled={busySubgroup === c.id}
                        title="Delete"
                        className="p-1.5 text-gray-400 hover:text-red-600 rounded disabled:opacity-40">
                        <Trash2 size={12} />
                      </button>
                    </div>
                  )}
```

with:

```tsx
                  {(can("groups", "manage_members") || can("groups", "edit")) && (
                    <div className="flex items-center gap-0.5 shrink-0">
                      {can("groups", "manage_members") && (
                        <button
                          onClick={e => { e.preventDefault(); setAddMemberGroupId(c.id); }}
                          disabled={busySubgroup === c.id}
                          title="Add member"
                          className="p-1.5 text-gray-400 hover:text-brand-600 rounded disabled:opacity-40">
                          <UserPlus size={12} />
                        </button>
                      )}
                      {can("groups", "edit") && (
                        <>
                          <button
                            onClick={e => { e.preventDefault(); handleArchiveSubgroup(c.id); }}
                            disabled={busySubgroup === c.id}
                            title="Archive"
                            className="p-1.5 text-gray-400 hover:text-amber-600 rounded disabled:opacity-40">
                            <Archive size={12} />
                          </button>
                          <button
                            onClick={e => { e.preventDefault(); handleDeleteSubgroup(c.id, c.name); }}
                            disabled={busySubgroup === c.id}
                            title="Delete"
                            className="p-1.5 text-gray-400 hover:text-red-600 rounded disabled:opacity-40">
                            <Trash2 size={12} />
                          </button>
                        </>
                      )}
                    </div>
                  )}
```

Note: the archived-subgroup icon block (unarchive/delete only, no add-member icon) stays unchanged — it has nothing to split.

- [ ] **Step 3: Switch the member-row delete button to `manage_members`**

Replace:

```tsx
                        <td className="px-4 py-3 text-right">
                          {can("groups", "edit") && (
                            <button onClick={() => removeMember(m)} className="text-gray-400 hover:text-red-600 p-1">
                              <Trash2 size={15} />
                            </button>
                          )}
                        </td>
```

with:

```tsx
                        <td className="px-4 py-3 text-right">
                          {can("groups", "manage_members") && (
                            <button onClick={() => removeMember(m)} className="text-gray-400 hover:text-red-600 p-1">
                              <Trash2 size={15} />
                            </button>
                          )}
                        </td>
```

- [ ] **Step 4: Type-check and build**

Run: `cd dashboard && npm run build`

Expected: build succeeds with no TypeScript errors.

- [ ] **Step 5: Manually verify in the browser**

Run: `cd dashboard && npm run dev`. Using the Roles page from Task 2, create a test role with `groups.view` + `groups.manage_members` but NOT `groups.edit`, assign it to a test user, log in as that user, and confirm on a group detail page: the "Add member" button and subgroup add-member icons are visible and work; the "Edit" button and subgroup archive/delete icons are hidden; the member-row trash icon is visible and works. Then flip the role to `groups.edit` only (no `manage_members`) and confirm the inverse.

- [ ] **Step 6: Commit**

```bash
git add "dashboard/src/app/(dashboard)/groups/[id]/page.tsx"
git commit -m "feat: gate group member add/remove on groups.manage_members instead of groups.edit"
```
