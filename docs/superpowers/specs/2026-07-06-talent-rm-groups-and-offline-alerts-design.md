# Talent / Relation-Manager guardrails, Users split, offline alerts, and subgroup member visibility

**Date:** 2026-07-06
**Status:** Approved (design) — pending spec review before implementation planning

## 1. Overview

Four related enhancements to the TimeTracker dashboard, all built on existing
architecture. **No new database tables, no schema migration, no email subsystem,
and no new background worker are required.**

1. **Talent guardrail** — Talent users (and, by construction, their devices)
   cannot be dashboard viewers / Relation Managers.
2. **Users module split** — a clear Team vs Talent separation via top-level tabs.
3. **Offline talent-device alerts** — alert a group's managers in-app when a
   talent device has been offline longer than a configurable threshold (default
   30 min).
4. **Subgroup member visibility** — show a group's members including members
   inherited from its subgroups.

## 2. Existing architecture (facts this design relies on)

**Stack:** FastAPI + SQLAlchemy 2.0 async + PostgreSQL (raw `.sql` migrations,
not Alembic). Next.js App Router + TypeScript + SWR + Tailwind + lucide-react.
Redis is response-cache only. Background work uses plain `asyncio`; only
`ingest_queue` is actually started in the FastAPI lifespan
(`backend/app/main.py:85`).

**Roles / RBAC:** Per-company rows in the `roles` table
(`backend/app/db/migrations/002_rbac.sql`). Three seeded system roles
(`backend/app/db/seed_rbac.py:21-31`):
- **Super Admin** — `scope_type="all"`, all permissions.
- **Viewer** — `scope_type="groups"`, read-only, group-scoped.
- **Talent** — `scope_type="own"`, `home.view` only.

`Employee.role_id → roles.id`; `scope_type ∈ {all, groups, own}`.
`get_current_user` (`backend/app/api/deps.py:129-182`) attaches transient
`_perms: set[(module, action)]` and `_scope_type` to the user;
`require(module, action)` (`deps.py:189-198`) gates endpoints.

**Talent detection** (`backend/app/core/provisioning.py:12`):
`TALENT_ROLE_NAME = "Talent"`. A user is Talent when its role is the system
Talent role — the authoritative check used elsewhere is
`role.is_system AND role.name == "Talent"` (see
`backend/app/api/v1/endpoints/googleAuth.py:284-297`). Talent users have
`password_hash IS NULL` and cannot log into the dashboard; they are
auto-provisioned on device enrollment
(`provisioning.link_or_provision_employee`).

**"Relation Manager" (RM) = a group-scoped viewer:** an employee whose role has
`scope_type="groups"` with `employee_group_access` (EGA) rows
(`backend/app/db/models.py:185`, `EmployeeGroupAccess`) granting view of specific
groups. EGA is assigned in exactly one endpoint: `PUT /users/{id}/groups`
(`backend/app/api/v1/endpoints/users.py:566`). Group-scoped users only see
employees/groups within their assigned groups + descendants
(`users.py:108-127`, `groups.py:69-79`).

**Groups:** `groups` with optional `parent_id` for nesting
(`002_rbac.sql:24`, `021_group_subgroups.sql`). `group_members` rows are either
an employee or a device (`002_rbac.sql:35-46`). Helpers live in
`backend/app/core/group_tree.py`:
- `descendant_group_ids(roots, company_id, db)` — root ids + all descendants.
- `devices_for_groups(group_ids, db)` — distinct device ids from direct members
  (covers both device members and devices of employee members).
- `build_paths(...)` — "Root / Child / Leaf" breadcrumbs.

**Devices:** `backend/app/db/models.py:54` — `employee_id`, `hostname`,
`last_seen`, `is_active`, `is_archived`, `is_deleted`. Online is computed
everywhere as `last_seen > now() − settings.ONLINE_CUTOFF_SECS`
(`ONLINE_CUTOFF_SECS = 60`, `backend/app/core/config.py:135`). This 60s "live
dot" cutoff is unrelated to the (much larger) offline-alert threshold.

**Notifications:** in-app **computed** feed
(`backend/app/api/v1/endpoints/notifications.py`). `GET ""` builds live
notifications (currently only `approval_request` for pending registrations);
`POST /mark-read` records read receipts in the `notification_reads` table
(`005_notification_reads.sql`, model `NotificationRead`). There is **no stored
notifications table** and **no email/SMTP** anywhere in the backend. Frontend:
`dashboard/src/components/NotificationBell.tsx`.

**Settings:** `companies.settings JSONB` (`031_perf_and_settings.sql:62`) edited
via `PATCH /company/settings` (`backend/app/api/v1/endpoints/company.py:123`).
Keys are whitelisted in `DEFAULT_SETTINGS` (`company.py:16-33`) and type-checked
in `_clean_settings` (`company.py:44-57`). The whitelist **already contains**
`offline_alert_mins` (currently default 15, range 0–1440) and `email_alerts` —
both stored/shown in the settings UI's Notifications tab but not consumed by any
code. Settings UI: `dashboard/src/app/(dashboard)/settings/page.tsx` (tabs at
`:18`, Notifications tab body `:456-500`). RBAC: `settings/view`, `settings/edit`.

## 3. Feature 1 — Talent users cannot be Relation Managers

### Goal
Only non-Talent users can be dashboard viewers / RMs. Devices are never viewers
(no login, no EGA) — already guaranteed, no code needed.

### Backend
In `PUT /users/{id}/groups` (`users.py:566`), before writing EGA rows, load the
target employee's role and **reject with `400`** if the target is the system
Talent role: message `"Talent users can't be assigned as Relation Managers.
Change the user's role first."` Belt-and-suspenders: also reject when the target
role's `scope_type != "groups"` (EGA is meaningless for `all`/`own`) — with a
clear message.

The check is on the **current** role at assignment time.

### Explicit non-restriction (user requirement)
The **role-change path (`PATCH /users/{id}`) stays unrestricted.** An admin must
always be able to convert a Talent user to any role. The intended flow is:
change role away from Talent → then assign group access. Nothing about Feature 1
may block editing a Talent user's role.

### Frontend
- In the user create/edit modal (`users/page.tsx`, `UserModal`) and the group
  access assignment UI (`GroupPicker` / `PUT users/{id}/groups` caller): when the
  selected user is Talent, hide/disable the **group-access** controls only.
- Keep the **role dropdown fully editable** for Talent users.
- Surface the backend 400 as an inline error if it is ever hit.

### Edge cases
- User with `role_id = NULL` ("monitored only", not Talent): treated as non-Talent
  by the Talent check, but caught by the `scope_type != "groups"` guard (no role →
  no groups scope), so they also can't be assigned EGA. Acceptable.
- Removing all group access from a user is unaffected.

## 4. Feature 2 — Users module Team vs Talent split (top-level tabs)

### Goal
Clearly separate monitored Talent from dashboard logins (admins / RMs / viewers).

### Backend
Add a computed field `user_kind: "talent" | "team"` to the users list response
(`UserOut`, `users.py` `list_users` at `:84` and the returned model). Derivation:
`"talent"` when the employee's role is the system Talent role
(`role_obj.is_system and role_obj.name == "Talent"`), else `"team"`. This keeps
the "what is a talent" rule in one place (backend) instead of re-deriving it in
the UI.

No change to filtering/scoping semantics; group-scoped viewers still only see
their in-scope employees.

### Frontend
`users/page.tsx`: tabs become **Team · Talent · Pending** (currently Active ·
Pending, `:236`, `:324`). Team = approved non-talent users; Talent = approved
talent users; Pending unchanged. Search (`smartMatch`) filters within the active
tab. Reuse existing columns; per-tab column tweaks are optional and not required.

Add `user_kind` to the `User` type (`dashboard/src/types/index.ts`).

## 5. Feature 3 — Offline talent-device alerts (in-app, computed, configurable)

### Goal
When a talent's device has been offline longer than the configured threshold,
show an in-app alert to the managers responsible for that talent's group(s).

### Mechanism — computed feed (no worker, no table)
Extend `notifications.py` `list_notifications` to also emit `talent_offline`
notifications, computed live on each poll (same pattern as `approval_request`).
No background worker and no new table.

**Recipient scoping (falls out of who is polling):**
- **Super Admin** (`_scope_type == "all"`): all talent devices company-wide.
- **Relation Manager** (has EGA rows): talent devices within the RM's assigned
  groups + descendants — via `descendant_group_ids(assigned_gids, ...)` then
  `devices_for_groups(...)`.
- A user who is neither Super Admin nor has any EGA rows gets no talent-offline
  notifications.

**Offline predicate** (per company `offline_alert_mins`):
a device qualifies when it is a **talent device** and
`last_seen IS NOT NULL AND last_seen < now() − offline_alert_mins`
and `is_active = TRUE AND is_archived = FALSE AND is_deleted = FALSE`.
- Talent device = `device.employee_id` points to an employee whose role is the
  system Talent role.
- `last_seen IS NOT NULL` is required so freshly enrolled devices that have never
  sent a heartbeat don't alert (the alert means "went offline", not "never
  online").
- Archived devices are excluded (they're handled by the device janitor concept).

**Gating:** only compute this section when `offline_alert_enabled` is true and
`offline_alert_mins > 0`.

**Read receipts:** key each alert `talent_offline:{device_id}:{last_seen_iso}`
(use `:never` if `last_seen` is null but that path is excluded above). Because
the key embeds `last_seen`, marking it read silences the *current* offline
incident; if the device reconnects (new `last_seen`) and later drops again, a
fresh unread alert appears.

**Notification payload** — human-readable, **no UUIDs / backend identifiers in
visible text**:
- `type`: `"talent_offline"`
- `title`: `⚠ {Talent name}'s device is offline`
- `body`: `{hostname} · offline for {N} min · {Group / Sub-group path} · last seen {local time}`
  - `{N}` = whole minutes since `last_seen`; `{path}` from `build_paths` for the
    group the device is reached through (nearest in the RM's scope; for Super
    Admin, the device's group path if any, else the talent's name only).
- `is_read`: from `notification_reads`.
- `metadata`: may include `device_id` **only** for a "view device" deep link in
  the UI; it is not rendered as text. No other backend fields.

**Performance:** bounded, indexed queries. Super Admin: talent devices in company
filtered by `last_seen`. RM: restricted to devices in their group subtree. Reuses
existing helpers/indexes; comparable cost to the existing approval-request query.

### Setting (Admin → Settings → Notifications)
Reuse the existing `offline_alert_mins` key; **change its default from 15 to 30**
in `DEFAULT_SETTINGS` (`company.py:16-33`). Add a new boolean
`offline_alert_enabled` (default `true`) to `DEFAULT_SETTINGS` and to `_BOOL_KEYS`
(`company.py:35-36`). `offline_alert_mins` stays in `_INT_KEYS` (range 0–1440).
**No migration** — both are JSONB settings keys.

Note: the default change (15→30) only affects companies that have **no** stored
`offline_alert_mins` value, since reads merge stored values over defaults
(`_read_extras`). A company that previously saved the Notifications tab may have
`15` persisted and will keep it until an admin edits it — acceptable, and the new
toggle makes the current value explicit in the UI.

Frontend (`settings/page.tsx` Notifications tab, `:456-500`): a labeled control —
toggle "Alert managers when a talent device goes offline" + a minutes input
(disabled when the toggle is off). Add both keys to the settings form state and
to the `updateCompanySettings` payload.

### Trade-off (recorded, decided)
Computed feed vs. persisted worker + `alerts` table. Chosen: **computed**, because
it needs zero new infra, is self-healing (device returns → alert disappears), and
matches the existing feed. It has no persistent alert history and no
"fired-at-moment-of-crossing" event; those are only needed for email/audit, which
are explicitly out of scope now (YAGNI). If email or an audit trail is wanted
later, that is when a worker + `alerts` table would be added.

## 6. Feature 4 — Group members including subgroups

### Goal
On a group's detail page, optionally show members inherited from subgroups, not
just direct members.

### Backend
Add `include_subgroups: bool = False` query param to
`GET /groups/{group_id}/members` (`groups.py:387`). When true, resolve
`descendant_group_ids([group_id], company_id, db)` and return members across the
whole subtree. Each returned member gains:
- `group_id` / `group_name` — the group the membership row belongs to.
- `path` — breadcrumb via `build_paths`.
- `direct: bool` — true if the membership is on the target group itself, false if
  inherited from a subgroup.

Extend `MemberOut` (`backend/app/schemas/rbac.py`) with the optional
`group_id`/`group_name`/`path`/`direct` fields (kept optional/back-compatible so
the non-subgroup call is unchanged). Preserve existing label resolution
(employee name / device hostname). Respect existing group visibility scoping via
`_get_group` on the root.

### Frontend
`groups/[id]/page.tsx`: add an **"Include subgroups"** toggle near the members
list.
- **Off** (default): current behavior — direct members only.
- **On**: fetch with `include_subgroups=true` and render members grouped into
  labeled sections by subgroup `path` (direct members section first), each section
  showing a count. Inherited members get a subtle badge/label indicating the
  source subgroup. Chosen for legibility over a heavyweight tree widget; reads
  well for deep hierarchies.

Add the param to the members fetch in `dashboard/src/lib/api.ts`.

## 7. Non-goals (YAGNI)

- No email / SMTP / push delivery. In-app feed only.
- No persisted `alerts` table and no new background worker.
- No change to how online/offline is computed elsewhere (`ONLINE_CUTOFF_SECS`
  live-dot logic stays as-is).
- Not starting the pre-existing-but-unstarted `device_janitor` / `report_cache`
  / `tech_cache` workers (noted below as out of scope).
- No restriction on changing a user's role (explicit requirement).

## 8. Cross-cutting notes

- **Unstarted workers (out of scope, flagged):** `device_janitor`,
  `report_cache`, `tech_cache` define `start()` loops but are never invoked in
  `main.py`'s lifespan (only `ingest_queue` is). This design does not depend on
  them and does not fix them; noted so it isn't mistaken for a regression.
- **Single source of truth for "is Talent":** implement one backend helper
  (e.g. in `provisioning.py` or `group_tree.py`) that decides Talent from a role
  (`is_system and name == TALENT_ROLE_NAME`) and reuse it in Features 1, 2, 3.

## 9. Testing approach

- **Feature 1:** unit/endpoint tests — assigning EGA to a Talent user → 400;
  to a `scope_type="own"`/`NULL`-role user → 400; to a Viewer/groups-scoped user
  → 200. `PATCH /users/{id}` changing a Talent user's role → still succeeds.
- **Feature 2:** `list_users` returns correct `user_kind` for Talent vs team vs
  monitored-only; frontend tab filtering.
- **Feature 3:** given a talent device with `last_seen` older than threshold,
  the feed emits one `talent_offline` alert for a Super Admin and for an RM whose
  group contains the device, and none for an out-of-scope user; disabled toggle
  or `mins=0` emits none; never-seen device emits none; read-receipt key changes
  when `last_seen` changes; no UUID appears in title/body.
- **Feature 4:** `include_subgroups=true` returns direct + inherited members with
  correct `direct` flag and paths; `=false` unchanged; visibility scoping honored.
- Follow existing test/migration conventions (run via Docker per project setup).

## 10. Touchpoints summary

**Backend**
- `backend/app/api/v1/endpoints/users.py` — EGA guard (F1), `user_kind` (F2).
- `backend/app/api/v1/endpoints/notifications.py` — `talent_offline` feed (F3).
- `backend/app/api/v1/endpoints/company.py` — settings defaults/whitelist (F3).
- `backend/app/api/v1/endpoints/groups.py` — `include_subgroups` (F4).
- `backend/app/schemas/rbac.py` — `MemberOut` extra fields (F4); `UserOut`
  `user_kind` (F2).
- `backend/app/core/provisioning.py` (or `group_tree.py`) — shared `is_talent`
  helper.

**Frontend**
- `dashboard/src/app/(dashboard)/users/page.tsx` — Team/Talent/Pending tabs (F2),
  hide group-access for Talent (F1).
- `dashboard/src/app/(dashboard)/groups/[id]/page.tsx` — include-subgroups toggle
  + grouped rendering (F4).
- `dashboard/src/app/(dashboard)/settings/page.tsx` — offline-alert toggle +
  minutes (F3).
- `dashboard/src/components/NotificationBell.tsx` — render `talent_offline`
  (icon/link) (F3).
- `dashboard/src/lib/api.ts` — members `include_subgroups` param (F4), settings
  payload keys (F3).
- `dashboard/src/types/index.ts` — `user_kind` on `User`, member extra fields.

**No SQL migration required.**
