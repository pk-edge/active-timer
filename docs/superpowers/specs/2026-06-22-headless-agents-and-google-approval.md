# Headless Agents + Google First-Time Approval — Design

**Date:** 2026-06-22 · **Status:** Approved (design Q&A answered); implementing.

## Feature 1 — Headless / visible agents (admin-controlled)

Decisions: **company-wide default + per-device override**; headless hides **both** the
floating widget and the tray icon (fully invisible); changes propagate **live (~60s)**
without an agent restart.

### Data (migration 019)
- `companies.agent_headless BOOLEAN NOT NULL DEFAULT FALSE` — company-wide default.
- `devices.headless_override BOOLEAN` (nullable) — NULL = inherit company; TRUE/FALSE = override.
- **Effective headless** = `COALESCE(device.headless_override, company.agent_headless)`.

### Backend
- `GET /v1/agent/config` — device-authed (like ingest). Returns `{ "headless": <effective> }`.
  Agent polls every ~60s. Cheap single-row read.
- `PATCH /v1/company/settings` — `settings:edit`. Sets `agent_headless`.
- Per-device override on the devices endpoint (`devices:assign`/`edit`): set TRUE/FALSE/inherit.
- `GET /v1/company/info` gains `agent_headless`; device list/detail gains effective `headless`
  + raw `headless_override` for the UI.

### Agent
- New `ConfigWorker` daemon thread: polls `/v1/agent/config` every ~60s with the device JWT,
  writes `shared["headless"]`. Never fatal — a failed poll keeps the last value.
- `main.py` seeds `shared["headless"]` once at startup before showing the widget.
- The widget's existing QTimer tick reads `shared["headless"]` and applies it on the Qt main
  thread (no cross-thread UI calls): `headless` → `hide()` widget + `tray.hide()`; else show.
- Anti-tamper close-veto unchanged — hide is not close.

### Dashboard
- Settings: a real, persisted "Make agents invisible (headless)" company toggle → PATCH.
- Devices list/detail: per-device control — Inherit / Visible / Hidden.

## Feature 2 — Admin approval for first-time Google sign-in

Google OAuth and the whole approval system already exist (`is_approved`, password-login gate,
Users → Pending tab, Approve modal, login "pending" notice). Only the first-time Google path
needs to change.

### Change (googleAuth.py callback)
- **Existing user** — matched by `google_sub` OR by email — and `is_active`:
  log in exactly as today (link `google_sub` to the row if it was missing). *Old user → in.*
- **Brand-new user** (no match): create with `is_approved=False`, `role_id=None`,
  `google_sub` set, `is_active=True` (mirrors self-registration). Do **not** issue a usable
  session.
- **Approval gate:** if the resolved user is `is_approved=False`, redirect to the login page
  with the existing pending flag instead of issuing the JWT. (Password login already gates
  `is_approved`; this mirrors it for Google.)
- Existing infra does the rest: the new user shows in Users → Pending; an admin Approves
  (assigning a role + groups); the user can then sign in with Google.

### No new UI for Feature 2
Reuses the existing Pending tab, Approve modal, and login pending notice.

## Testing
- Backend: effective-headless resolution (company default vs device override) via `/v1/agent/config`;
  PATCH company settings; per-device override; Google callback — new user → pending (no session,
  appears pending), existing user → logs in, email-match links google_sub.
- Agent: ConfigWorker poll → `shared["headless"]`; widget headless apply (hide/show) is a thin
  Qt shell, device-validated.
