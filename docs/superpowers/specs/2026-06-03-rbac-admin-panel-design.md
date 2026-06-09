# Role-Based Admin Panel, Groups & Agent Management — Design

**Date:** 2026-06-03
**Status:** Approved (design); pending implementation plan
**Scope:** Backend (FastAPI/SQLAlchemy/Postgres), Frontend (Next.js 14), Desktop agent (Python)

---

## 1. Goal

Turn the TimeTracker dashboard into a fully role-based, self-serve admin panel modeled on ActivTrak:

- **Users** — list, search, manually add, edit, deactivate, assign role + scope + groups.
- **Groups** — create (named), list, detail with members table, add/remove members (users *and* devices), CSV import.
- **Roles** — fully customizable: admins create named roles and toggle a permission matrix (modules × actions) + a data scope. Nothing hardcoded. Multiple admins supported.
- **Data scoping (core purpose)** — a Relationship Manager (RM) who logs in sees **only the data of the groups assigned to them**, never other groups. Driven entirely by role scope + group assignment.
- **Agent management** — download per-OS installers (DMG/MSI/EXE) from the dashboard; admins upload a new installer to publish an agent update; agents self-update by polling an update-check endpoint.

**Design principles:** nothing hardcoded (roles, permissions, modules, groups all data-driven); company-scoped multi-tenant-safe; minimal human intervention; every new surface fully wired end-to-end.

## 2. Confirmed decisions

1. **RBAC model:** Custom roles + permission matrix. Admins define roles and per-module/per-action permissions.
2. **Data detail:** Module access controls visibility. "Full / partial / selective" = *which modules* a role can open. Within an allowed module the user sees full detail for their scoped data. (No summary-only mode or field-level redaction in this version.)
3. **Group membership:** Mixed — a group contains employees and/or specific devices. An RM sees devices that are directly in their groups **plus** devices owned by employees in their groups.
4. **Agent update:** Upload + dashboard download + agent self-update check.
5. **Monitored employees without login** appear in the Users list as manageable records (assignable to groups; no password/role required). One `employees` table; login is optional per user.
6. **Single company per deployment** (seeded via `seedAdmin`). All data stays `company_id`-scoped for safety, but no public multi-tenant signup flow is built.

## 3. Current system (verified facts the design builds on)

- `employees` already has `role TEXT DEFAULT 'employee'` (values `employee|manager|admin`), `password_hash` (nullable), `is_active`. `get_current_admin` decodes the admin JWT and checks `type=='admin'` + `is_active` — **it does not check role**. Login (`/v1/auth/admin/login`) allows `role in ('admin','manager')` with a valid password.
- All data queries filter by `admin.company_id`. Hook points for scoping: `devices.list_devices`, `devices._get_device_for_admin`, `devices.get_timeline`, `devices.get_daily_stats`, `reports.*`, `logs.get_logs`, `screenshots.*`, the SSE `/devices/stream`.
- `logs.get_logs` builds its WHERE via f-string interpolation of clause *names* (params are still bound) — scoping filter must use a bound `= ANY(:ids)` param, not interpolation.
- Migrations are idempotent SQL applied from `db/base.py` (`001_initial.sql`). New tables ship as `002_rbac.sql` using `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`.
- Agent enrolls with the company `enrollment_token`, receives `device_id` + `jwt_token`, persists to `identity.json`; API base + token are baked into the installer at build time. Agent sends `agent_version` (currently hardcoded `"1.0.0"`).
- Backend already has an S3/MinIO client (`screenshots.py:_s3_client`) and `S3_ENDPOINT_URL` / `S3_PUBLIC_ENDPOINT_URL` / `PRESIGNED_URL_EXPIRY` settings — reused for installer storage.
- Frontend: token in `localStorage("tt_token")`; `request()` wrapper; `useAuth()` guard; flat `Sidebar` NAV array; login ignores `role`/`name`. No role gating exists yet.

## 4. Data model (new) — migration `002_rbac.sql`

All tables use UUID PKs (`gen_random_uuid()`), `company_id` FK to `companies(id) ON DELETE CASCADE`, and follow existing index-naming conventions.

```
roles
  id            UUID PK
  company_id    UUID FK companies(id) ON DELETE CASCADE
  name          TEXT NOT NULL
  description   TEXT NULL
  scope_type    TEXT NOT NULL DEFAULT 'all'      -- 'all' | 'groups' | 'own'
  is_system     BOOLEAN NOT NULL DEFAULT FALSE   -- system roles cannot be deleted
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
  UNIQUE(company_id, name)
  idx_roles_company (company_id)

role_permissions
  id            UUID PK
  role_id       UUID FK roles(id) ON DELETE CASCADE
  module        TEXT NOT NULL   -- devices|logs|reports|screenshots|users|groups|roles|agents|settings|audit
  action        TEXT NOT NULL   -- view|create|edit|delete
  UNIQUE(role_id, module, action)
  idx_role_permissions_role (role_id)

groups
  id            UUID PK
  company_id    UUID FK companies(id) ON DELETE CASCADE
  name          TEXT NOT NULL
  description   TEXT NULL
  created_by    UUID FK employees(id) ON DELETE SET NULL NULL
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
  UNIQUE(company_id, name)
  idx_groups_company (company_id)

group_members
  id            UUID PK
  group_id      UUID FK groups(id) ON DELETE CASCADE
  member_type   TEXT NOT NULL   -- 'employee' | 'device'
  employee_id   UUID FK employees(id) ON DELETE CASCADE NULL
  device_id     UUID FK devices(id) ON DELETE CASCADE NULL
  added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
  CHECK ((member_type='employee' AND employee_id IS NOT NULL AND device_id IS NULL)
      OR (member_type='device'   AND device_id   IS NOT NULL AND employee_id IS NULL))
  partial UNIQUE idx (group_id, employee_id) WHERE employee_id IS NOT NULL
  partial UNIQUE idx (group_id, device_id)   WHERE device_id   IS NOT NULL
  idx_group_members_group (group_id)

employee_group_access            -- which groups an RM may VIEW (scope='groups')
  id            UUID PK
  employee_id   UUID FK employees(id) ON DELETE CASCADE
  group_id      UUID FK groups(id) ON DELETE CASCADE
  UNIQUE(employee_id, group_id)
  idx_ega_employee (employee_id)

installer_versions
  id             UUID PK
  company_id     UUID FK companies(id) ON DELETE CASCADE
  platform       TEXT NOT NULL   -- 'macos' | 'windows'
  installer_type TEXT NOT NULL   -- 'dmg' | 'msi' | 'exe'
  version        TEXT NOT NULL   -- semver
  s3_key         TEXT NOT NULL
  file_size      BIGINT NOT NULL DEFAULT 0
  sha256         TEXT NULL
  notes          TEXT NULL
  is_active      BOOLEAN NOT NULL DEFAULT FALSE
  uploaded_by    UUID FK employees(id) ON DELETE SET NULL NULL
  uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
  idx_installer_versions_company (company_id, platform)
  partial UNIQUE idx (company_id, platform) WHERE is_active   -- one active per OS

audit_log
  id                UUID PK
  company_id        UUID
  actor_employee_id UUID NULL
  action            TEXT NOT NULL   -- e.g. user.create, role.update, group.add_member, installer.activate
  target_type       TEXT NULL
  target_id         TEXT NULL
  metadata          JSONB NULL
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
  idx_audit_company_time (company_id, created_at DESC)
```

**Altered table:** `employees` gains `role_id UUID FK roles(id) ON DELETE SET NULL NULL`. The legacy `role` string column is retained for backward compatibility.

**Seed (in migration or `db/base.py` bootstrap):** for each existing company, create a `Super Admin` role (`scope_type='all'`, `is_system=true`, all module/action permissions) and a `Viewer` starter role; set `role_id` of existing `admin`/`manager` employees to Super Admin. Idempotent (guard on role name existence).

## 5. Permission catalog (data-driven)

A single backend constant defines the matrix so the UI never hardcodes it:

```
MODULES = [
  {key:'devices',     label:'Devices',      actions:['view']},
  {key:'logs',        label:'Activity Logs',actions:['view']},
  {key:'reports',     label:'Reports',      actions:['view']},
  {key:'screenshots', label:'Screenshots',  actions:['view']},
  {key:'users',       label:'Users',        actions:['view','create','edit','delete']},
  {key:'groups',      label:'Groups',       actions:['view','create','edit','delete']},
  {key:'roles',       label:'Roles',        actions:['view','create','edit','delete']},
  {key:'agents',      label:'Agents',       actions:['view','create','edit','delete']},
  {key:'settings',    label:'Settings',     actions:['view','edit']},
  {key:'audit',       label:'Audit Log',    actions:['view']},
]
```

Exposed via `GET /v1/roles/modules`. Adding a module later = one entry here, zero UI changes.

## 6. Backend — scoping engine, permission gate, endpoints

### 6.1 Scoping engine (single source of truth)

`app/core/scoping.py`:

```python
async def visible_device_ids(user, db) -> list[UUID] | "ALL":
    role = user's role (loaded with permissions)
    if role.scope_type == 'all':   return ALL            # sentinel → no device filter, company filter only
    if role.scope_type == 'own':   return device ids where employee_id == user.id
    if role.scope_type == 'groups':
        gids = select group_id from employee_group_access where employee_id = user.id
        return union(
          select device_id from group_members where group_id in gids and member_type='device',
          select d.id from devices d join group_members gm on gm.employee_id = d.employee_id
                 where gm.group_id in gids and gm.member_type='employee'
        )
```

Returns the `ALL` sentinel for admins (so existing company-only filtering stays optimal), or an explicit id list for scoped users. Endpoints apply `AND device_id = ANY(:ids)` only when not `ALL`. Empty list → user sees nothing (valid for an RM with no groups yet).

### 6.2 Permission dependency

`app/api/deps.py`:

- `get_current_user` (extends `get_current_admin`) — loads the Employee plus `role` and `role_permissions` (selectinload), attaches a `perms: set[(module,action)]` and `scope_type`.
- `require(module, action)` — dependency factory returning **403** if `(module,action)` not in `perms`. Super Admin (`scope_type='all'` with full matrix) passes everything.
- Backward compat: if `role_id` is null but legacy `role in ('admin','manager')`, treat as full-permission `all` scope so existing seeded admin keeps working until migrated.

### 6.3 New / changed endpoints (prefix `/v1`)

| Method | Path | Permission | Purpose |
|--------|------|-----------|---------|
| POST | `/auth/admin/login` | — | Widened: any active employee with password + role logs in. Returns `{access_token, name, role_name}`. |
| GET | `/auth/me` | authenticated | `{id,name,email,role_name,scope_type,permissions:[{module,action}]}` for UI gating. |
| GET | `/roles/modules` | roles.view | Permission catalog. |
| GET/POST | `/roles` | roles.view / roles.create | List / create role (+ permissions + scope_type). |
| GET/PATCH/DELETE | `/roles/{id}` | roles.* | Read / edit matrix+scope / delete (block if `is_system` or assigned). |
| GET/POST | `/users` | users.view / users.create | List (search/filter) / create (name,email,role_id,scope groups, optional password, is_monitored). |
| GET/PATCH/DELETE | `/users/{id}` | users.* | Read / edit (role, activate, set password) / soft-delete (`is_active=false`). |
| PUT | `/users/{id}/groups` | users.edit | Set the groups an RM may view (writes `employee_group_access`). |
| GET/POST | `/groups` | groups.view / groups.create | List (with member counts) / create (name, description). |
| GET/PATCH/DELETE | `/groups/{id}` | groups.* | Detail (info + members) / rename / delete. |
| GET/POST/DELETE | `/groups/{id}/members[/{member_id}]` | groups.view / groups.edit | List / add (employee or device) / remove. |
| POST | `/groups/import` | groups.create | CSV upload → create groups + assign members by email/hostname. |
| GET/POST | `/installers` | agents.view / agents.create | List versions / upload (multipart → MinIO + row). |
| POST | `/installers/{id}/activate` | agents.edit | Mark active for its platform. |
| GET | `/installers/{id}/download` | agents.view | Presigned MinIO download URL. |
| GET | `/updates/check` | device JWT | Agent self-update: compares `agent_version` to active version for its platform → `{update_available, version, download_url}`. |

### 6.4 Wire scoping into existing endpoints

`devices.py` (`list_devices`, `_get_device_for_admin`, `get_timeline`, `get_daily_stats`), `reports.py` (all queries), `logs.py` (`get_logs` — via bound `= ANY(:ids)`), `screenshots.py`, and the SSE `/devices/stream` all gain the `visible_device_ids` filter on top of the current `company_id` filter. Each also gets the appropriate `require(module,'view')` dependency.

## 7. Agent self-update

`agent/updater.py`: on launch and once daily, GET `/v1/updates/check` (device JWT). If `update_available` and the active `version` is newer than local `__version__`:
- Download installer to a temp path.
- macOS: `hdiutil attach`, copy `.app` to `/Applications`, `hdiutil detach`, relaunch.
- Windows: run `msiexec /i installer.msi /quiet /qn /norestart` (or EXE `/VERYSILENT`), using the existing `protection/win_guard.py` watchdog to replace the running binary.
- Gated by a Config flag (`AUTO_UPDATE_ENABLED`, default on). `__version__` centralized in one module.

## 8. Frontend — permission-aware UI

- `hooks/useUser.ts` + a context provider: on mount, fetch `/v1/auth/me`; expose `{name, roleName, scopeType, can(module, action)}`. Cached; cleared on logout/401.
- `Sidebar` NAV entries gain a `module` field; rendered list is filtered by `can(module,'view')`. New entries: Users, Groups, Roles, Agents (alongside Devices, Logs, Reports, Settings).
- Auth gate moved into the dashboard layout (removes the redirect flash).
- New pages under `app/(dashboard)/`:
  - `users/page.tsx` — searchable/filterable table; "Add User" modal (name, email, role select, scope-group multiselect when role scope='groups', optional password, monitored toggle); row actions edit / deactivate / reset password / assign groups.
  - `groups/page.tsx` — "Create Group" entry (New Group modal **or** Upload CSV) → groups table (name, members, created). `groups/[id]/page.tsx` — group info card + members table + "Add Member" picker (search users and devices) + remove.
  - `roles/page.tsx` — roles table; create/edit drawer with a permission-matrix grid (modules × actions checkboxes) sourced from `/roles/modules` + scope_type radio.
  - `agents/page.tsx` — active version per OS, drag-drop upload (platform/type/version/notes), version history table, per-OS download + activate.
- Existing devices/logs/reports pages need no scoping code — backend returns only permitted data. They only gain nav-level gating.
- New API client functions + TS types for users, roles, groups, members, installers, me.

## 9. Build phases (each independently verifiable)

1. **RBAC core** — `002_rbac.sql` (roles, role_permissions, employees.role_id), seed Super Admin, models, `MODULES` catalog, `get_current_user` + `require()`, `/auth/me`, `/roles` CRUD + `/roles/modules`, widened login. Apply `require()` to existing endpoints. *Verify:* migration applies; seeded admin logs in; `/auth/me` returns full permissions; a role with no perms gets 403.
2. **Scoping + Groups** — groups, group_members, employee_group_access tables; `scoping.py`; wire `visible_device_ids` into devices/reports/logs/screenshots/SSE; `/groups` + members + CSV import. *Verify:* an RM scoped to one group sees only that group's devices in every data endpoint; admin still sees all.
3. **User management** — `/users` CRUD + role/group/password assignment + soft delete. *Verify:* create RM, assign role+groups, log in as RM, confirm scoped view.
4. **Agent management** — `installer_versions`, upload/download/activate to MinIO, `/updates/check`, agent `updater.py`. *Verify:* upload installer, activate, agent check returns update, download URL works.
5. **Frontend** — `useUser` context, gated sidebar, Users/Groups/Roles/Agents pages, API client + types. *Verify:* `tsc` + `next build` clean; nav reflects role; pages CRUD against backend.
6. **Audit log + end-to-end** — `audit_log` writes on all mutations + read-only viewer (audit.view); full RM-vs-admin walkthrough. *Verify:* mutations recorded; complete flow works.

## 10. Error handling & edge cases

- Missing permission → 403 with clear detail; frontend hides the nav entry and guards the page.
- RM with no assigned groups → empty (not all) data; UI shows an empty state, not an error.
- Deleting a role that is assigned → blocked with 409; must reassign users first. System roles cannot be deleted.
- Group/role name uniqueness enforced per company (409 on conflict).
- CSV import: per-row validation; report created/skipped/errored counts; unknown emails/hostnames skipped with a reason.
- Installer upload: validate platform/type/extension and size; compute sha256; only one `is_active` per platform (activating one deactivates the rest in a transaction).
- Device with `employee_id = NULL` is only visible via direct device membership in a group (or to `all`-scope users).
- Backward compat: legacy seeded admin (string `role='admin'`, null `role_id`) retains full access until linked to Super Admin by the seed step.

## 11. Out of scope (YAGNI)

- Summary-only / field-level data redaction (deferred; module access is the visibility control for now).
- Public multi-tenant company signup/onboarding.
- SSO / OAuth logins; per-user API keys.
- Agent rollback / staged rollout channels (single active version per OS).
