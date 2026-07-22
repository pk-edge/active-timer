# Group "Manage Members" Permission — Design Spec

**Date:** 2026-07-08
**Status:** Approved

---

## Problem

Adding or removing a user (or device) from a group/subgroup is currently gated by the same permission as renaming a group or moving it in the tree: `groups.edit`. Admins have no way to grant a role the ability to add/remove group members without also granting it the ability to restructure groups (rename, reparent).

---

## Non-Goals

- No change to `POST /v1/groups/import` (CSV bulk import) — it stays gated on `groups.create`, unchanged. It is a distinct bulk-creation flow, not the per-member add/remove UI action this spec addresses.
- No change to group create/delete/archive permissions.
- No DB schema change — `role_permissions.action` is already a free-form string column with no CHECK constraint, so a new action value needs no migration beyond seeding data.

---

## Permission Catalog

Add one new action to the existing `groups` module in `backend/app/core/permissions.py`:

| Module | Action | Grants |
|---|---|---|
| `groups` | `manage_members` | Add a member to a group/subgroup; remove a member from a group/subgroup |

`groups.edit` narrows to: rename, change description, move parent (reparent).

---

## Backend Changes

### `backend/app/core/permissions.py`

```python
{"key": "groups", "label": "Groups", "actions": ["view", "create", "edit", "delete", "manage_members"]},
```

### `backend/app/api/v1/endpoints/groups.py`

Two endpoints switch their `require(...)` dependency from `("groups", "edit")` to `("groups", "manage_members")`:

- `POST /{group_id}/members` (`add_member`)
- `DELETE /{group_id}/members/{member_id}` (`remove_member`)

All other endpoints (`create_group`, `update_group`, `delete_group`, `archive_group`, `unarchive_group`, `import_groups`) keep their current permission requirements unchanged.

---

## Backfill Migration

New file `backend/app/db/migrations/032_group_manage_members_permission.sql`, following the pattern established in `012_backfill_super_admin_perms.sql`:

```sql
INSERT INTO role_permissions (role_id, module, action)
SELECT rp.role_id, 'groups', 'manage_members'
FROM role_permissions rp
WHERE rp.module = 'groups' AND rp.action = 'edit'
ON CONFLICT (role_id, module, action) DO NOTHING;
```

This guarantees every role that could already add/remove members (by virtue of holding `groups.edit`) keeps that ability after deploy — no silent capability loss. Super Admin needs no row: `deps.py:get_current_user` grants it every `(module, action)` pair dynamically (`all_permissions()`) whenever `role_obj.scope_type == "all" and role_obj.is_system`, independent of seeded rows.

---

## Frontend Changes

### `dashboard/src/app/(dashboard)/roles/page.tsx`

The `RoleEditor` permission matrix hardcodes its columns to `["view", "create", "edit", "delete"]` (the table already renders `—` for any module lacking a given action, via `m.actions.includes(a)`). Add `"manage_members"` as a fifth column. Because the raw action string does not read well under the existing CSS `capitalize` (`manage_members` → `Manage_members`), add a small label map so the header renders `"Members"` while the other four columns keep rendering their raw (already word-shaped) key as today.

### `dashboard/src/app/(dashboard)/groups/[id]/page.tsx`

Today a single `can("groups", "edit")` check gates four unrelated things at once: the "Edit" button, the "Add member" button, the per-subgroup add-member icon (bundled with archive/delete icons), and the member-row delete (trash) button. Split the gate:

| UI element | Old gate | New gate |
|---|---|---|
| "Edit" button (rename modal) | `groups.edit` | `groups.edit` (unchanged) |
| "Add member" button (group header) | `groups.edit` | `groups.manage_members` |
| Subgroup row — add-member icon | `groups.edit` | `groups.manage_members` |
| Subgroup row — archive/delete icons | `groups.edit` | `groups.edit` (unchanged) |
| Member table row — delete (trash) icon | `groups.edit` | `groups.manage_members` |

The subgroup action-icon cluster (add-member, archive, delete) currently renders as one `can("groups","edit") && (...)` block; it splits into two independently-gated fragments so a role with only `manage_members` sees the add-member icon without archive/delete, and vice versa.

---

## Files Changed

| File | Change |
|---|---|
| `backend/app/core/permissions.py` | Add `manage_members` action to `groups` module |
| `backend/app/api/v1/endpoints/groups.py` | `add_member`/`remove_member` require `groups.manage_members` |
| `backend/app/db/migrations/032_group_manage_members_permission.sql` | New migration — backfill existing `groups.edit` holders |
| `dashboard/src/app/(dashboard)/roles/page.tsx` | Add `manage_members` column + label map to permission matrix |
| `dashboard/src/app/(dashboard)/groups/[id]/page.tsx` | Split `groups.edit` gate into `groups.edit` / `groups.manage_members` per element above |

---

## Testing

- Existing backend tests (`test_e2eApplication.py`, `test_group_members_subgroups.py`) authenticate as an all-scope admin token, which holds every permission dynamically — unaffected by this change, should continue to pass as-is.
- Manual verification: create a role with `groups.manage_members` but not `groups.edit`; confirm it can add/remove group members via the API and dashboard, but cannot rename/reparent a group or archive/delete a subgroup.
- Manual verification: a role holding only `groups.edit` (no `manage_members`) can rename/reparent/archive/delete but the "Add member" button and member-row delete icon are hidden, and the underlying API calls 403.

---

## Safety Checklist

- No DB schema change — `action` column already accepts arbitrary strings.
- Migration is a pure backfill `INSERT ... ON CONFLICT DO NOTHING` — idempotent, zero rows touched or removed.
- No existing role loses capability on deploy (backfill runs before the endpoint gate changes take effect).
- Super Admin unaffected — permission set computed dynamically, not from seeded rows.
