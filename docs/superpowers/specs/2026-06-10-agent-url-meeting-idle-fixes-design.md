# Agent URL Fetching, Meeting/Video Detection & Activity Logging Fixes

**Date:** 2026-06-10
**Scope:** agent (Windows + macOS), backend ingest, dashboard timeline

## Problems reported

1. Agent is not fetching browser URLs.
2. Meetings / video watching are not reported.
3. Meeting/video time incorrectly counts as idle.
4. Activity logs must flow to the backend continuously.
5. App info (window titles) must be correct. Any browser must work
   (Safari, Chrome, Firefox, Edge, Brave, Arc).

## Root causes found (verified on a live device)

| # | Root cause | Evidence |
|---|-----------|----------|
| 1 | `UrlWatcher.run()` / `WindowWatcher.run()` / `InputMonitor` loops have **no exception handling** — one transient error (e.g. SQLite lock) kills the thread for the rest of the session. All internal errors log at DEBUG, invisible at INFO level. | Local queue.db: last `url_visit` 13:00:47; agent restarted 13:05:10, ran 45+ min, user browsed (verified in Chrome history), zero new `url_visit` rows. Manual replay of the same SQL works. |
| 2 | Backend `ALLOWED_EVENT_TYPES` does not include `meeting_start` / `meeting_end` / `url_blocked` which the agent emits. Pydantic validator rejects the **entire batch** with 422; sync worker retries the same oldest batch forever → device sync jams permanently. | `backend/app/schemas/event.py` whitelist vs. event types emitted in `window_watcher.py` / `url_watcher.py`. |
| 3 | `LocalDB.insert_event` ignores `event["ts"]` and stamps insert time — URL visits lose their real visit timestamps (all visits in a poll share one ts). | 14 url_visit rows all stamped `13:00:47`. |
| 4 | `_copy_locked_db` copies only the main DB file. Firefox `places.sqlite` and Safari `History.db` run in WAL mode — recent visits live in the `-wal` sidecar and are invisible in the copy. | SQLite WAL semantics; Safari/Firefox History DBs are WAL. |
| 5 | macOS window titles require Screen Recording permission (`kCGWindowName`); never requested → 97% of captured events have empty titles → browser media/meeting detection by title can never fire on Mac. | queue.db: 2397 empty titles vs 68 non-empty. |
| 6 | Meeting detection only matches **native app names** (zoom, teams…). Meetings in a browser (Google Meet, Zoom web, Teams web) are never detected. Media title keywords are too narrow (`meet.google.com` never appears in a tab title — real titles look like "Meet – abc-defg-hij"). | `window_watcher.MEETING_APPS`, `Config.MEDIA_BROWSER_TITLES`. |
| 7 | Events are only emitted on window *change* — staying in one app/tab for an hour produces zero logs (and active-time SUM lags by the full stay). | WindowWatcher emit logic; backend `SUM(duration)` of `window_change`. |

## Design

### Backend (`backend/app`)

- `schemas/event.py`: stop raising in the validator. Unknown event types are
  **skipped per-event** at the endpoint (counted in `skipped`), never 422 for
  the batch. Whitelist expands to include `meeting_start`, `meeting_end`,
  `url_blocked`. Forward-compatible: a newer agent can never jam an older
  backend again.
- `endpoints/ingest.py`: filter rows by the whitelist, count skipped.

### Agent (`agent/`)

- **Crash-proofing:** every watcher loop body wrapped in try/except logging at
  WARNING with the exception; thread never dies. Applies to UrlWatcher,
  WindowWatcher, InputMonitor (all platforms).
- **`LocalDB.insert_event`** honors `event["ts"]`, falls back to now.
- **`_copy_locked_db`** copies `-wal` / `-shm` / `-journal` sidecars next to
  the temp copy so WAL browsers (Firefox, Safari) expose recent visits.
  Logs discovered browser DBs once at INFO; warns once when Safari history
  exists but is unreadable (needs Full Disk Access).
- **Meeting/video context** (single source of truth in WindowWatcher):
  - `_is_meeting_context(app, title)` = native meeting app OR browser whose
    title matches meeting patterns (google meet, zoom, teams, webex…).
  - `_is_media_context(app, title)` = media app (Spotify…) OR browser title
    matching media patterns (YouTube, Netflix, Prime Video, Twitch…).
  - WindowWatcher publishes `shared["in_meeting"]` / `shared["media_active"]`
    and emits `meeting_start` / `meeting_end` (browser meetings included).
  - InputMonitor consults the shared flags first (fallback: own title check)
    → meeting/video time is never marked idle, on both platforms.
- **Heartbeat slices:** WindowWatcher emits a `window_change` slice for the
  *current* app every `ACTIVITY_HEARTBEAT_SECS` (60 s) even without a window
  switch. Durations are emitted as deltas since the last emit, so backend
  `SUM(duration)` stays exact while logs flow continuously. While
  `shared["is_idle"]` is set (published by InputMonitor) slices are
  suppressed and the slice clock resets — idle time no longer inflates
  active time. Reuses the `window_change` type so older backends accept it.
- **macOS window titles:**
  - At startup, preflight Screen Recording access (`CGPreflightScreenCaptureAccess`)
    and request it once if missing (Quartz titles then work for all apps).
  - Fallback: when frontmost app is a known browser and the Quartz title is
    empty, fetch the active tab title via AppleScript (Chrome-dictionary
    browsers + Safari). Failures are cached so a denied Automation prompt
    doesn't spam.
  - Windows titles already work via `GetWindowTextW` (no change).

### Dashboard

- `ActivityTimeline.tsx`: icon/colour entries for `meeting_start`,
  `meeting_end`, `url_blocked` so meetings are visible on the device page.

## Error handling

- All watcher failures log WARNING and continue next cycle.
- AppleScript/permission failures degrade to empty title (never crash).
- Unknown event types: skipped server-side, counted in response.

## Testing

- New agent tests: `insert_event` ts honor; WAL sidecar copy; meeting/media
  context classification (browser titles, native apps).
- New backend test: ingest accepts meeting events, skips unknown types
  without rejecting the batch.
- Manual: run agent from source, verify url_visit/meeting events appear in
  queue.db and sync 202s.
