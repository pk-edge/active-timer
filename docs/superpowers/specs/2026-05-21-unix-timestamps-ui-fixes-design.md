# Unix Timestamps + UI Fixes + Windows MSI Parity — Design Spec

**Date:** 2026-05-21  
**Status:** Approved  

---

## Problem Statement

Six bugs share the same root cause — timestamps represented as ISO 8601 strings introduce timezone ambiguity at every parse point:

1. `fmtTime()` in the dashboard displays times in the viewer's browser timezone instead of the recorded timezone, causing times to shift by hours.
2. The logs and timeline date filters use `date=YYYY-MM-DD` interpreted as UTC midnight, so users in UTC+ zones see "no results" for today because their local date is ahead of UTC.
3. A `±1 day` window hack was added to the date filter as a band-aid — it works but masks the root cause.
4. `DailyChart.tsx` appends `T12:00:00` to date strings to avoid DST edge cases — another hack.
5. Windows MSI never writes `api_base.txt` to `_internal/`, so `config.py`'s file-based resolution falls through to the env var, which can be cleared externally.
6. Device detail page date picker initialises to today (local), triggering the same UTC mismatch as the logs page.

---

## Approach: Unix Integer Timestamps at Transport + API Layer (Approach B)

### Guiding principles

- **Store UTC, display local.** Postgres keeps `TIMESTAMPTZ` (already UTC-correct). The agent stores unix seconds in SQLite. The API returns unix integer seconds. The frontend converts to local time using `new Date(ts * 1000)` — the browser handles the timezone conversion automatically and correctly.
- **Remove all hacks, not just symptoms.** The ±1 day window, the `T12:00:00` append, the `replace("Z", "+00:00")` string manipulation — all go away.
- **Windows and macOS identical.** Both installers write `api_base.txt` to the platform bundle directory. No platform-specific code paths in `config.py`.

---

## Section 1: Agent

### `agent/db/local_db.py`

- Change SQLite schema: `ts INTEGER NOT NULL` (unix seconds) instead of `ts TEXT NOT NULL`.
- Same for `captured_at INTEGER NOT NULL` and session `started_at INTEGER NOT NULL` / `ended_at INTEGER`.
- On startup, run a one-time migration: if `ts` column is `TEXT`, add `ts_unix INTEGER`, backfill via `CAST(strftime('%s', ts) AS INTEGER)`, rename columns. Increment `PRAGMA user_version` from 1 → 2 to gate the migration.
- New events: store `int(time.time())` directly.

### `agent/sync/sync_worker.py`

- Ingest payload `"timestamp"` field: send the integer directly — no `.isoformat()`, no string formatting.
- Screenshot payload `"captured_at"`: same — integer unix seconds.

---

## Section 2: Backend

### `backend/app/schemas/event.py`

- `EventIn.timestamp`: change type to `Union[int, str]`. If `int`, convert via `datetime.fromtimestamp(ts, tz=timezone.utc)`. If `str`, keep existing `fromisoformat` parse (backwards compat for one release).

### `backend/app/api/v1/endpoints/ingest.py`

- Handle both int and str timestamp in the parse block. No other changes.

### `backend/app/api/v1/endpoints/logs.py`

- Replace `date: Optional[str]` query param with `since: Optional[int]` and `until: Optional[int]` (unix seconds).
- Filter: `ae.timestamp >= to_timestamp(:since) AND ae.timestamp < to_timestamp(:until)`.
- Default (no params): `since = now - 172800` (48 hours ago). No `until` = open-ended.
- Remove the ±1 day workaround entirely.
- Add `timestamp_unix: int` to each returned row (via `EXTRACT(EPOCH FROM ae.timestamp)::bigint`).

### `backend/app/api/v1/endpoints/devices.py` (timeline)

- Replace `date: Optional[str]` with `since`/`until` unix params. Same logic as logs.
- Add `timestamp_unix: int` to `TimelineEventOut`.

### `backend/app/api/v1/endpoints/devices.py` (device list)

- Add `last_seen_unix: Optional[int]` to `DeviceOut` — computed as `int(d.last_seen.timestamp())` if `last_seen` is set.

### `backend/app/schemas/`

- Update `TimelineEventOut`, `LogEvent` (if defined in schema), `DeviceOut` to include `*_unix` integer fields.
- Keep ISO string fields for this release (drop in a future cleanup).

---

## Section 3: Dashboard (Frontend)

### `dashboard/src/lib/api.ts`

- `fmtTime(ts: number): string` — `new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })`. Takes unix seconds. Browser applies local timezone automatically.
- `fmtDate(ts: number): string` — `new Date(ts * 1000).toLocaleDateString()`.
- `getLogs(opts)` — replace `date: string` param with `since?: number, until?: number`. When a date is selected, compute `since = Math.floor(new Date(selDate).getTime() / 1000)`, `until = since + 86400`.
- `getTimeline(deviceId, since?, until?)` — same pattern.

### `dashboard/src/types/index.ts`

- `LogEvent`: add `timestamp_unix: number`.
- `TimelineEvent`: add `timestamp_unix: number`.
- `Device`: add `last_seen_unix: number | null`.

### `dashboard/src/app/(dashboard)/logs/page.tsx`

- `selDate` initialises to `""` (no preselection — already fixed).
- When date is selected: compute `since`/`until` and pass to `getLogs()`.
- All `fmtTime(e.timestamp)` → `fmtTime(e.timestamp_unix)`.

### `dashboard/src/app/(dashboard)/devices/[id]/page.tsx`

- Date picker: on change compute `since`/`until` from selected date and pass to `getTimeline()`.
- `fmtTime` call sites updated to use `_unix` fields.

### `dashboard/src/app/(dashboard)/devices/_components/DeviceCard.tsx`

- `formatDistanceToNow(new Date(device.last_seen_unix * 1000))` using `last_seen_unix`.

### `dashboard/src/app/(dashboard)/devices/[id]/_components/ActivityTimeline.tsx`

- `fmtTime(e.timestamp_unix)`.

### `dashboard/src/app/(dashboard)/devices/[id]/_components/ScreenshotGrid.tsx`

- `fmtTime(s.captured_at_unix)` — requires `captured_at_unix` added to screenshot type and returned by backend.

### `backend/app/api/v1/endpoints/screenshots.py` (list endpoint)

- Add `captured_at_unix: int` to `ScreenshotOut` schema, computed as `int(ss.captured_at.timestamp())`.

### `dashboard/src/app/(dashboard)/devices/[id]/_components/DailyChart.tsx`

- Remove the `T12:00:00` append. Chart x-axis uses `YYYY-MM-DD` date strings from the backend `stats` endpoint — these are already date-only (no time component), so no timezone shift is possible. `format(parseISO(d), "EEE")` from `date-fns` is safe for date-only strings.

---

## Section 4: Windows MSI

### `installer/windows/build_msi.ps1`

After the PyInstaller step completes and `$DistDir` exists, add:

```powershell
# Bake API base into _internal/ so config.py finds it (matches macOS behaviour)
[System.IO.File]::WriteAllText(
    "$DistDir\_internal\api_base.txt",
    $ApiBase,
    (New-Object System.Text.UTF8Encoding $false)
)
```

### `installer/windows/TimeTracker.wxs`

No change — the existing `<Environment>` component for `TT_API_BASE` stays as a secondary fallback.

### `agent/config.py`

No change — `_resolve_api_base()` already checks `_internal/api_base.txt` on Windows. Once the build script writes the file, it resolves correctly.

---

## Data Flow (after changes)

```
Agent captures event
  → stores int(time.time()) in SQLite ts column
  → sync_worker sends {"timestamp": 1716229409, ...} to POST /v1/ingest
  → backend: datetime.fromtimestamp(1716229409, tz=utc) → stores as TIMESTAMPTZ

GET /v1/logs?since=1716192000&until=1716278400
  → backend filters ae.timestamp in [since, until)
  → returns [{..., "timestamp_unix": 1716229409}, ...]

Dashboard: fmtTime(1716229409)
  → new Date(1716229409 * 1000).toLocaleTimeString()
  → "07:30 PM" (user's local time, correct)
```

---

## Files Changed

| Layer | File | Change |
|-------|------|--------|
| Agent | `agent/db/local_db.py` | Schema + migration: TEXT ts → INTEGER ts |
| Agent | `agent/sync/sync_worker.py` | Send int timestamp in payload |
| Backend | `backend/app/schemas/event.py` | `timestamp: Union[int, str]` |
| Backend | `backend/app/api/v1/endpoints/ingest.py` | Parse int or str timestamp |
| Backend | `backend/app/api/v1/endpoints/logs.py` | `since`/`until` params, `timestamp_unix` in response |
| Backend | `backend/app/api/v1/endpoints/devices.py` | `since`/`until` for timeline, `last_seen_unix` in device list, `timestamp_unix` in timeline |
| Backend | `backend/app/schemas/device.py` | Add `timestamp_unix`, `last_seen_unix` fields |
| Dashboard | `dashboard/src/lib/api.ts` | `fmtTime(ts: number)`, `since`/`until` params |
| Dashboard | `dashboard/src/types/index.ts` | `*_unix` fields on LogEvent, TimelineEvent, Device |
| Dashboard | `dashboard/src/app/(dashboard)/logs/page.tsx` | Date → unix conversion, use `timestamp_unix` |
| Dashboard | `dashboard/src/app/(dashboard)/devices/[id]/page.tsx` | Date → unix for timeline |
| Dashboard | `dashboard/src/app/(dashboard)/devices/_components/DeviceCard.tsx` | `last_seen_unix` |
| Dashboard | `dashboard/src/app/(dashboard)/devices/[id]/_components/ActivityTimeline.tsx` | `timestamp_unix` |
| Backend | `backend/app/api/v1/endpoints/screenshots.py` | Add `captured_at_unix` to list response |
| Dashboard | `dashboard/src/app/(dashboard)/devices/[id]/_components/ScreenshotGrid.tsx` | `captured_at_unix` |
| Dashboard | `dashboard/src/app/(dashboard)/devices/[id]/_components/DailyChart.tsx` | Remove `T12:00:00` hack |
| Installer | `installer/windows/build_msi.ps1` | Write `_internal/api_base.txt` |

---

## What Does NOT Change

- Postgres column types stay as `TIMESTAMPTZ` — no migration.
- ISO string fields in API responses are kept alongside `_unix` fields (removed in a future cleanup pass).
- macOS `build_dmg.sh` — already correct.
- `agent/config.py` — already correct.
- `TimeTracker.wxs` env var component — stays as fallback.
