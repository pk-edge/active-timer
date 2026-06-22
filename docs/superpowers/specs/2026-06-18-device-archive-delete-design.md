# Device Archive & Soft Delete — Design Spec

**Date:** 2026-06-18  
**Status:** Approved

---

## Problem

Admins need to suspend or remove devices from the active fleet without losing historical activity data. Two levels of removal are required:

- **Archive** — temporary suspension; device cannot send data, can be restored.
- **Delete** — permanent removal from the UI; treated as soft-delete in the database so historical events are preserved.

---

## Non-Goals

- No hard-delete of any DB rows (activity events, screenshots, daily summaries are all preserved).
- No destruction of existing production data — the migration is purely additive.
- No change to agent-side behaviour (agents do not know about archive/delete states).

---

## Data Model

### Migration 017 — additive only

```sql
ALTER TABLE devices
  ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN is_deleted  BOOLEAN NOT NULL DEFAULT FALSE;
```

All existing rows get `is_archived=FALSE, is_deleted=FALSE` via the DEFAULT — no data touched.

### Device state invariant

| State    | `is_active` | `is_archived` | `is_deleted` |
|----------|-------------|---------------|--------------|
| Active   | `true`      | `false`       | `false`      |
| Archived | `false`     | `true`        | `false`      |
| Deleted  | `false`     | `false`       | `true`       |

`is_active = TRUE` only when both new columns are `FALSE`. Any write to archive or delete must also set `is_active = FALSE`.

---

## Ingest Gate (no code change)

`deps.py:get_current_device` already rejects devices where `is_active = FALSE`. Because archive and delete both set `is_active=False`, **archived and deleted devices are immediately blocked from sending any events** — no change required.

---

## Enrollment Re-registration (`POST /auth/enroll`)

The enroll endpoint upserts by `(company_id, hostname)`. New behaviour:

| Existing device state | Action |
|---|---|
| Active | Existing behaviour — update token, reset `is_active=True` |
| **Archived** | Update token (agent gets valid credential) but **do not** set `is_active=True`. Agent can re-enroll but cannot ingest until admin restores. |
| **Deleted** | Ignore the deleted record, **create a new Device row**. Agent gets a fresh identity. Deleted record stays soft-deleted. |

Implementation: change the upsert lookup to `WHERE company_id = :cid AND hostname = :h AND is_deleted = FALSE`. If no row found (because only a deleted row exists), fall through to the `not device` branch and create fresh.

For archived: after the existing upsert block, add a guard: if `device.is_archived`, skip setting `is_active = True`.

---

## API Changes

### `GET /devices` (modified)

- Default (unchanged): returns only `is_active=TRUE, is_deleted=FALSE` devices.
- New query param: `?show_archived=true` — also returns `is_archived=TRUE` devices alongside active ones. Requires `("devices", "view")` permission (already required).
- Deleted devices **never** appear in any list response.
- `DeviceOut` schema gains two new fields: `is_archived: bool`, `is_deleted: bool`.

### `PATCH /devices/{id}/archive` (new)

- Permission: `("devices", "archive")`
- Precondition: device must be active (`is_active=TRUE`, `is_deleted=FALSE`).
- Effect: `is_active=False, is_archived=True`. Employee assignment is preserved.
- Audit: `device.archived`.
- Response: updated `DeviceOut`.

### `PATCH /devices/{id}/restore` (new)

- Permission: `("devices", "archive")`
- Precondition: device must be archived (`is_archived=TRUE`).
- Effect: `is_active=True, is_archived=False`.
- Audit: `device.restored`.
- Response: updated `DeviceOut`.

### `DELETE /devices/{id}` (new)

- Permission: `("devices", "delete")`
- Precondition: device must not already be deleted (`is_deleted=FALSE`).
- Effect: `is_deleted=True, is_active=False`. No DB row removed.
- Audit: `device.deleted`.
- Response: `204 No Content`.

---

## Data Isolation

Archived and deleted devices must not appear in any active reporting or home view.

| Location | Current behaviour | Change needed |
|---|---|---|
| `GET /devices` (default) | Filters `is_active=TRUE` | Add `is_deleted=FALSE` for safety; toggle archived via `show_archived` |
| `home.py` raw SQL | `WHERE d.is_active = TRUE` | Already correct — no change |
| `devices.py _get_device_for_admin` | No `is_deleted` check | Add `is_deleted=FALSE` — deleted devices return 404 |
| `reports.py _get_device_for_admin` | No `is_active`/`is_deleted` check | Add `is_deleted=FALSE` — deleted devices return 404 on timeline/stats |
| `scoping.py visible_device_ids` (own scope) | No `is_active` filter | Add `Device.is_active==True` — prevents archived/deleted device IDs leaking into talent home view |

Historical activity events, screenshots, and daily summaries remain in the database for both archived and deleted devices.

---

## Permissions

Two new permission types required:

| Module | Action | Grants |
|---|---|---|
| `devices` | `archive` | Archive a device; restore from archive |
| `devices` | `delete` | Soft-delete a device |

Super-admin role must be backfilled with both permissions (migration 017 or a seed update).

---

## Files Changed

| File | Change |
|---|---|
| `backend/app/db/migrations/017_device_archive_delete.sql` | New migration — additive columns + permission seed |
| `backend/app/db/models.py` | Add `is_archived`, `is_deleted` to `Device` |
| `backend/app/schemas/device.py` | Add `is_archived`, `is_deleted` to `DeviceOut` |
| `backend/app/api/v1/endpoints/devices.py` | `list_devices` toggle; 3 new endpoints; guard in `_get_device_for_admin` |
| `backend/app/api/v1/endpoints/auth.py` | Enrollment re-registration logic for deleted/archived |
| `backend/app/core/scoping.py` | Add `is_active==True` filter in "own" scope query |
| `backend/app/api/v1/endpoints/reports.py` | Add `is_deleted==False` to device lookup |

---

## Safety Checklist

- Migration is purely `ADD COLUMN … DEFAULT FALSE` — zero rows touched, zero downtime risk.
- `is_active` remains the single ingest gate — no change to the hot path.
- Deleted devices can re-register as fresh identities — agent continuity preserved.
- All historical data (events, screenshots, summaries) kept forever — no data loss.
