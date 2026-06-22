# Meeting Logs — Design

**Date:** 2026-06-22
**Status:** Approved (design decided via brainstorming Q&A); implementing.

## Goal

Track how long each user spends **in meetings**, cross-platform (macOS + Windows),
accurately enough that it does **not** need manual on-device testing to trust.

"Meeting time" (decided with the user):
- = the **whole session** from when a call starts until it ends,
- **includes** time spent in other apps during the meeting (those apps are part of
  the meeting),
- **excludes** locked / screen-off time,
- is **not** cut short by idle (a quiet listener is still in the meeting).

## Detection — window/tab signals only (no microphone)

Each poll the agent gathers two cheap snapshots and feeds them to the pure core:
- **windows**: list of `{owner, title}` for visible windows.
- **tabs**: list of `{url, title}` for browser tabs (macOS via the existing
  url_watcher AppleScript; Windows = best-effort foreground tab only).

`detect_presence(windows, tabs)` returns a `MeetingPresence(platform, title)` or
`None`:
- **Native call windows** (both OS) — owner+title match a known signature:
  Zoom Meeting/Webinar, Microsoft Teams call, Webex meeting, Slack huddle.
- **Browser in-call tabs** — URL matches an in-call pattern: Google Meet
  (`meet.google.com/<xxx-xxxx-xxx>`), Teams web (`meetup-join`/`/l/meetup-join`),
  Zoom web (`/wc/` or `/j/`). A bare `meet.google.com/` lobby is **not** a call.
- Native takes priority over browser when both match.

Signatures live as **data** in the module so they are tunable without touching
logic; the exact native title strings are the one part that needs device
validation.

## Session model — the glitch-free core (`MeetingSessionTracker`)

Consumes `(presence|None, now)` each poll, emits `meeting_start` / `meeting_end`:
- **Start**: on first presence → `meeting_start(platform, title, ts)`.
- **Continue**: while presence is seen (any platform) keep the session open and
  advance `last_present_ts`. A disappearance shorter than `confirm_secs`
  (default 20s) does **not** end it → no flicker. A platform switch mid-session
  (Zoom→Meet) stays **one** session.
- **End**: once presence has been absent for `>= confirm_secs`, emit
  `meeting_end(platform, title, ts=last_present_ts, duration=last_present_ts-start_ts)`.
  The trailing confirm window is **not** counted.
- **Lock/sleep**: the agent's existing `pause_event` stops the watchers, so the
  tracker stops being fed → presence goes `None` → the session ends at the last
  in-call moment, excluding locked time. A resumed call after unlock starts a new
  session.

## Active-time + apps-used — computed backend-side

`meeting_start`/`meeting_end` are stored as ordinary `activity_events`
(`event_type` = `meeting_start`/`meeting_end`, `app_name` = platform,
`window_title` = title). **No new tables** (YAGNI — events suffice).

A session = `[start_ts, end_ts]`. On read the backend computes:
- **active time** = `SUM(window_change.duration)` for that device within the
  range (same formula as today-active; includes other apps; idle/lock contribute
  nothing because they emit no window_change),
- **apps used** = distinct `app_name` + per-app duration within the range.

Sessions whose active time is below a small threshold (30s) are dropped as noise.

## Surfacing

Per-user activity detail and per-device detail get a **Meetings** section: total
meeting time (today/range), session count, and a list — platform, start–end,
duration, active time, top apps used.

## Testability (why this won't need manual testing to trust)

- `agent/meeting_detect.py` is **pure** (no OS calls, no Qt): `detect_presence`
  + `MeetingSessionTracker`. Fully unit-tested: flicker, platform switch,
  back-to-back sessions, lock mid-meeting, zero-duration noise.
- Thin OS collectors (gather windows/tabs) are the only un-unit-testable part —
  kept minimal and device-validated.
- Backend aggregation is tested against **seeded** `meeting_*` + `window_change`
  events — no agent or device required.

## Integration risk to verify during build

The ingest path must **accept** the `meeting_start`/`meeting_end` event types
(there is a known event-whitelist jam hazard in the queue). Confirm both the
ingest endpoint and the agent's local queue pass these through before relying on
them.
