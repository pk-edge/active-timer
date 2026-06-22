# Desktop Agent Auto-Update — Design Spec

- **Date:** 2026-06-18
- **Status:** Draft for review
- **Source architecture:** [`Desktop_Agent_Auto-Update_Architecture.md`](../../../Desktop_Agent_Auto-Update_Architecture.md)
- **Scope:** Full spec (all 5 subsystems), integrated **additively** into the existing TimeTracker flow.

---

## 1. Goals & Non-Goals

### Goals
- Bring the existing agent auto-update path up to the production-grade design in the architecture doc: semantic version comparison, staged rollout, mandatory updates, minimum-supported-version gating, release channels, a **signed manifest**, integrity verification, resumable downloads, atomic install, rollback, crash/power-loss recovery, and end-to-end telemetry.
- Make per-company **distribution configurable and observable** in the admin panel (rollout %, mandatory, channel, min-version, plus fleet version-adoption + update status).
- Keep the system **fully functional and optimized** at fleet scale.

### Hard constraint — additive only
**No existing table, column, endpoint, API response shape, agent workflow, or build script changes its current behavior.** New behavior is introduced via new nullable columns (with today-equivalent defaults), new endpoints, new optional response fields, new agent code paths, and new tooling. Existing agents and existing data continue to behave exactly as they do today.

### Non-goals (explicitly deferred — see §11)
- Delta / per-file incremental updates (low value for a frozen PyInstaller bundle).
- OS-level code signing / notarization (Apple Developer ID, Windows Authenticode).
- A separate launcher **binary** (we use an updater **subprocess** of the existing binary instead).
- CDN provisioning (we build CDN-*ready*; delivery stays presigned-S3 for now).
- Full CI build of the agent (you build locally + run a publish tool; you own deployment).
- Multi-region distribution.

---

## 2. Decisions (confirmed with stakeholder)

| # | Decision | Choice |
|---|---|---|
| D1 | Release ownership | **Per-company** (keep current `company_id` scoping; no super-admin/cross-company model) |
| D2 | "Company-wise distribution in admin panel" | **Per-company controls scoped to the admin's own company** (consistent with all existing `agents` endpoints) |
| D3 | Code signing | **Ed25519 manifest signing only** + enforced sha256 on client; **no** OS code-signing/notarization |
| D4 | CDN | **CDN-ready** URL helper + `CDN_BASE_URL` config; presigned S3 delivery for now |
| D5 | Build/release | **Local build** (existing scripts unchanged) + new **publish tool**; stakeholder owns deployment |
| D6 | Release model storage | **Extend `installer_versions`** with additive columns (vs. new `releases`/`release_files` tables) |
| D7 | Agent updater process | **Updater subprocess** (`TimeTracker --update`), not a separate launcher binary |

### Faithful-but-adapted mapping to the architecture doc
| Architecture doc element | This design |
|---|---|
| `releases` + `release_files` tables | **Extended `installer_versions`** row (one installer file per release; no delta → no per-file table needed) |
| Separate Launcher binary + Updater process | **Updater subprocess mode** of the existing agent binary (no autostart/install change) |
| Signed manifest + `manifest.sig` | **Ed25519-signed canonical manifest**, public key embedded in the agent |
| CDN distribution | **`CDN_BASE_URL`-swappable URL helper**; presigned S3 today |
| Version API `GET /api/version` | **Enhanced existing `GET /v1/updates/check`** (additive fields) |
| `update_runs` table + telemetry events | **New `update_runs` table + `POST /v1/updates/report`** |
| Only-download-changed-files / delta | **Deferred**; full-installer download with resume + hash verify |

---

## 3. Current State (baseline — unchanged)

- **Agent:** PyInstaller bundle (PySide6). `agent/version.py` → `__version__`. `agent/updater.py::UpdateWorker` is a daemon thread (started in `agent/main.py`) that polls `GET /v1/updates/check` every `Config.UPDATE_CHECK_SECS` (6h), downloads the full DMG/MSI, and installs (macOS: mount DMG + `cp -R` to `/Applications`; Windows: `msiexec /i … /quiet /norestart`). No verify/resume/rollback/staging/restart. Identity in `identity.json`; enrollment via `/v1/auth/enroll`; autostart via `agent/autostart.py`.
- **Backend:** `installer_versions` (`id, company_id, platform, installer_type, version, s3_key, file_size, sha256, notes, is_active, uploaded_by, uploaded_at`) + `installer_downloads`. Endpoints: `/v1/installers` (list/upload/activate/delete/download/downloads), `/v1/updates/check` (device JWT), public `/v1/downloads/{enrollment_token}/{filename}`. Partial unique index `uniq_active_installer (company_id, platform) WHERE is_active`. `/v1/updates/check` compares **string equality** `active.version == device.agent_version`. S3 bucket `timetracker-screenshots`, prefix `installers/{company_id}/{platform}/{version}/`; `_s3_client(public)` helper. `Device.agent_version` set on enrollment. Next migration = **016**.
- **Dashboard:** Agents page — upload/list/activate/delete/download/public-links/download-history.
- **Build:** local `installer/macos/build_macos.sh`, `installer/windows/build_windows.ps1`, `agent/timetracker.spec`. No signing. CI builds backend+dashboard images only.
- **Secrets:** `app/core/secrets.py` provides `SECRETS_KEY`-based encryption (used for Google creds) — reused to store the Ed25519 private signing key.

---

## 4. Architecture Overview (additive layering)

```text
            ┌──────────────────────── Admin Dashboard (Agents page) ─────────────────────┐
            │  existing: upload / activate / delete / download / public links            │
            │  NEW: rollout % · mandatory · channel · min-version · fleet adoption view   │
            └───────────────┬────────────────────────────────────────────────┬───────────┘
                            │ existing /v1/installers (+ new PATCH fields)     │ NEW /v1/updates/runs (read)
                            ▼                                                  ▼
   ┌──────────────────────────── Backend (FastAPI) ─────────────────────────────────────┐
   │ installer_versions (+ additive cols)   update_runs (NEW)   Ed25519 signer (NEW)     │
   │ GET /v1/updates/check  (ENHANCED, backward-compatible)                              │
   │ GET /v1/updates/manifest/{id}  (NEW, signed)     POST /v1/updates/report  (NEW)     │
   └───────────────┬───────────────────────────────────────────────┬─────────────────────┘
                   │ presigned S3 (or CDN_BASE_URL later)            │ telemetry
                   ▼                                                 │
            S3 (installers/…)                                        │
                   │                                                 │
                   ▼                                                 │
   ┌──────────────────────────── Agent (existing binary) ───────────┴─────────────────────┐
   │ UpdateWorker thread (ENHANCED): jitter → check → fetch+verify signed manifest →       │
   │   resumable download → sha256 verify → stage → write update_state.json + update.lock  │
   │   → spawn `TimeTracker --update` subprocess → exit                                    │
   │ Updater subprocess (NEW mode): backup → atomic swap → relaunch → health check →       │
   │   rollback on failure → report telemetry → clear lock/state                           │
   └───────────────────────────────────────────────────────────────────────────────────────┘
                   ▲
                   │ new publish tool (local)
   ┌───────────────┴───────────── Release tooling (NEW, local) ───────────────────────────┐
   │ installer/release.py: hash installer → build canonical manifest → Ed25519 sign →      │
   │   upload to S3 → create/patch installer_versions row (rollout/mandatory/channel/…)    │
   └───────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. SP1 — Backend Release Intelligence

### 5.1 DB changes — migration `016_agent_releases.sql` (additive)
Add nullable columns to `installer_versions` (existing rows keep today's behavior via defaults):

| Column | Type | Default | Meaning |
|---|---|---|---|
| `rollout_percent` | `INT` | `100` | % of the platform's devices eligible (deterministic bucketing). Existing rows → 100 = everyone, as today. |
| `mandatory` | `BOOLEAN` | `FALSE` | Update is required (agent must not stay indefinitely on old version). |
| `channel` | `VARCHAR(20)` | `'stable'` | Release channel; device channel defaults to `stable`. |
| `min_supported_version` | `VARCHAR(50)` | `NULL` | Below this, update is forced regardless of rollout. |
| `manifest_json` | `TEXT` | `NULL` | Cached canonical manifest (string that was signed). |
| `manifest_sig` | `TEXT` | `NULL` | Base64 Ed25519 signature over `manifest_json`. |
| `expires_at` | `TIMESTAMPTZ` | `NULL` | Manifest expiry; agent rejects stale manifests. |
| `published_at` | `TIMESTAMPTZ` | `NULL` | When activated/published (telemetry + grace-period base). |

Constraints/indexes: keep `uniq_active_installer` as-is. Add `CHECK (rollout_percent BETWEEN 0 AND 100)`. No data migration (defaults suffice).

> **Backward-compat guarantee:** an existing active row has `rollout_percent=100, mandatory=false, channel='stable'`. With the enhanced `/check` logic (§5.4) those values reproduce today's "everyone on the active version updates" behavior exactly.

### 5.2 Canonical manifest format
Generated by the publish tool (§7), stored in `manifest_json`, served by the manifest endpoint. Canonicalization = JSON with sorted keys, UTF-8, no extra whitespace (so the signature is reproducible).

```json
{
  "schema": 1,
  "company_id": "<uuid>",
  "platform": "macos",
  "installer_type": "dmg",
  "version": "1.2.0",
  "channel": "stable",
  "mandatory": false,
  "min_supported_version": "1.0.0",
  "release_date": "2026-06-18T12:00:00Z",
  "expires_at": "2026-07-18T12:00:00Z",
  "files": [
    { "path": "TimeTracker.dmg", "sha256": "<hex>", "size": 25123456 }
  ]
}
```
`files[]` has exactly one entry today (the installer). The schema supports multiple for a future delta phase without a format break.

### 5.3 Ed25519 signing
- **Keypair:** generated once. **Private key** stored encrypted via `app/core/secrets.py` (same `SECRETS_KEY` mechanism as Google creds) and/or supplied via env `AGENT_SIGNING_PRIVATE_KEY` (base64). **Public key** committed into the agent (`agent/update_signing_key.py` as a base64 constant) and embedded at build time — the agent trusts **only** this key.
- **Library:** `cryptography` (already a backend dependency) `Ed25519PrivateKey`/`Ed25519PublicKey`. Agent verifies with the same lib (already vendored via PyInstaller; add to hidden imports if needed).
- **Signed bytes:** the exact `manifest_json` string. Signature base64 in `manifest_sig`.
- New helper `app/core/update_signing.py`: `sign_manifest(canonical_json) -> b64`, `public_key_b64()`.

### 5.4 Enhanced `GET /v1/updates/check` (backward-compatible)
Same auth (device JWT), same existing keys; **adds** fields. New server-side decision pipeline:

1. Resolve active `installer_versions` row for `device.company_id` + mapped platform (unchanged query).
2. **Semver compare** (new `app/core/semver.py`): update only if `active.version > device.agent_version` (parsed). If unparmeable, fall back to today's string-inequality (no regression).
3. **min_supported_version:** if `device.agent_version < min_supported_version` → force update (ignore rollout).
4. **Rollout bucketing (deterministic, sticky):** `bucket = int(sha256(f"{device.id}:{active.version}").hexdigest()[:8], 16) % 100`. Eligible iff `bucket < rollout_percent` **or** mandatory **or** forced-by-min-version. Stable per (device, target version) so a device doesn't fl/flop as the percent ramps.
5. **expiry:** if `expires_at` passed → treat as no active update (don't serve stale).

**Response (existing keys preserved, new keys added):**
```json
{
  "update_available": true,
  "version": "1.2.0",
  "installer_type": "dmg",
  "download_url": "<presigned-or-cdn>",
  "sha256": "<hex>",

  "mandatory": false,
  "min_supported_version": "1.0.0",
  "channel": "stable",
  "manifest_url": "https://api…/v1/updates/manifest/<id>",
  "manifest": { /* inline canonical manifest (§5.2) */ },
  "signature": "<b64 Ed25519>",
  "public_key_id": "v1"
}
```
Old agents read only the original keys and behave as today. New agents verify `signature` over `manifest` with the embedded key before downloading.

### 5.5 `GET /v1/updates/manifest/{id}` (new)
Returns `{ "manifest": <canonical json>, "signature": <b64> }` for an installer row (device JWT, company-scoped). Lets the agent re-fetch/verify independently and supports `manifest.cache` on the client.

### 5.6 Rollback (server)
"Withdraw a release" = re-activate the previous installer row (existing activate endpoint already deactivates others). The enhanced `/check` then serves the previous version; agents already on the withdrawn version are handled by min-version/mandatory if a forward-fix is needed. No new endpoint required; documented as an operational action + a dashboard button (§9).

### 5.7 Error handling & security (SP1)
- Presign failure → `update_available:false` (today's behavior).
- All transport HTTPS (prod). Manifest signed; agent rejects unsigned/expired/older-than-last-trusted.
- Company isolation preserved (all queries scoped by `company_id`).

---

## 6. SP2 — Telemetry

### 6.1 `update_runs` table — same migration 016 (new)
```sql
CREATE TABLE update_runs (
  id              UUID PRIMARY KEY,
  device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  company_id      UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  from_version    VARCHAR(50),
  target_version  VARCHAR(50) NOT NULL,
  platform        VARCHAR(20),
  channel         VARCHAR(20),
  status          VARCHAR(40) NOT NULL,     -- see event map
  error_code      VARCHAR(100),
  progress        INT DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_update_runs_company_created ON update_runs (company_id, created_at DESC);
CREATE INDEX idx_update_runs_device          ON update_runs (device_id, created_at DESC);
```

### 6.2 Status values (architecture-doc event map)
`CHECK_STARTED, CHECK_SUCCESS, CHECK_FAILED, MANIFEST_DOWNLOAD_STARTED, MANIFEST_DOWNLOAD_SUCCESS, MANIFEST_VERIFY_FAILED, FILE_DOWNLOAD_STARTED, FILE_DOWNLOAD_RESUMED, FILE_DOWNLOAD_SUCCESS, FILE_DOWNLOAD_FAILED, INSTALL_STARTED, INSTALL_SUCCESS, INSTALL_FAILED, ROLLBACK_STARTED, ROLLBACK_SUCCESS, ROLLBACK_FAILED`.

### 6.3 `POST /v1/updates/report` (new, device JWT)
Body: `{ target_version, from_version?, status, error_code?, progress?, platform?, channel? }`. Upserts a row keyed by (device_id, target_version) advancing `status`/`progress`; appends terminal events. Must **never** 5xx the agent into a failure loop — validation-lenient, returns 202. Rate/size limited.

### 6.4 Read API for dashboard
`GET /v1/updates/runs` (RBAC `agents:view`, company-scoped): recent runs + an aggregate (counts by status, version-adoption from `devices.agent_version`). Powers §9 fleet view.

---

## 7. SP3 — Release Tooling (`installer/release.py`, local)

A CLI you run **after** the existing build scripts produce a `.dmg`/`.msi` (those scripts are untouched).

```
python installer/release.py \
  --file dist/TimeTracker-1.2.0.dmg --platform macos --version 1.2.0 \
  --channel stable --rollout 25 [--mandatory] [--min-supported 1.0.0] \
  --api https://api.tracker.onedge.co --token <admin-jwt>
```
Steps: validate semver → sha256 + size → build canonical manifest (§5.2) → Ed25519-sign (private key from env/secrets) → upload installer to the existing S3 key layout → create/update the `installer_versions` row with manifest+signature+rollout fields (via a new admin endpoint `POST /v1/installers/{id}/manifest` or extended upload). Prints the row id + a summary. Idempotent on (company, platform, version).

> Keeps your "build locally, I deploy" model: the tool only publishes metadata + artifact; you decide when to activate/ramp via the dashboard.

---

## 8. SP4 — Agent Client (enhance `agent/updater.py`; new `--update` mode)

Only `agent/updater.py` (and a small new `agent/update_signing_key.py`) change. `main.py` thread startup, `version.py`, identity, enrollment, autostart, watchers — **untouched**. The existing `UpdateWorker` keeps its name + call site; its internals are upgraded.

### 8.1 State files (under `Config.BASE_DIR`)
`update_state.json` (`{status, version, progress, lastUpdated}`), `update.lock` (single-updater guard), `manifest.cache`, `last_successful_version.txt`, staging dir `updates/<version>/`, backup dir `backup/<version>/`.

### 8.2 Check → stage (in `UpdateWorker` thread)
1. **Jitter** before each check (random 0–N min) to avoid thundering herd (complements server bucketing).
2. `CHECK_STARTED` → call `/check` → `CHECK_SUCCESS/FAILED`.
3. If update: fetch manifest (inline or `manifest_url`), **verify Ed25519 signature** with embedded key, check `expires_at`, and reject any manifest **older than `last_successful_version`** (replay/downgrade guard) → `MANIFEST_VERIFY_FAILED` on any failure (abort, keep running).
4. **Disk-space pre-check** (download + staging + backup + overhead).
5. **Resumable download** to staging: HTTP range requests, exponential backoff w/ jitter (`5m→15m→30m→1h→4h→12h`), reuse a valid cached/partial file (dedupe by hash) → `FILE_DOWNLOAD_*`.
6. **Verify** sha256 + size vs manifest; mismatch/truncation → delete + retry.
7. Write `update_state.json=STAGED`, acquire `update.lock`. If user is active and update is **not** mandatory → defer install to idle/next launch.

### 8.3 Install → verify (new `--update` subprocess)
The agent spawns `TimeTracker --update --version X --staging …`, then exits cleanly (stops watchers/sync). The updater process:
1. `INSTALL_STARTED`. Wait for the agent to fully exit (lock handshake).
2. **Backup** current install to `backup/<old>/`.
3. **Atomic replace:**
   - **macOS:** stage new `TimeTracker.app` beside current, then atomic swap (`rename`/`renamex_np` exchange) into `/Applications`; never a half-copied app.
   - **Windows:** run MSI; rename-in-use handled by MSI; if a file is locked, retry/scheduled-replace.
4. **Relaunch** the new version; run a **health check** (process up + reports `INSTALL_SUCCESS` heartbeat within timeout).
5. **Success** → `INSTALL_SUCCESS`, update `last_successful_version.txt`, clear staging/lock/state.
6. **Failure / crash / failed health check** → `ROLLBACK_STARTED` → restore backup → relaunch old → `ROLLBACK_SUCCESS/FAILED` (leave recovery marker for next launch if rollback itself fails).

### 8.4 Recovery on launch
On every agent start, read `update_state.json`: if `INSTALLING` was interrupted (power loss/crash) → verify integrity, finish or roll back from backup before normal startup. `DOWNLOADING` → resume. Bounded retries (failure record; no infinite loop).

### 8.5 Mandatory / min-version
If `mandatory` or below `min_supported_version`: do not defer indefinitely; retry until success (still resumable + backoff). Optional user-facing "updating" notice (non-blocking).

### 8.6 Architecture-doc edge-case coverage (summary)
No internet / DNS / CDN-down → keep running, backoff retry. Partial/slow download → resume. Disk full → keep partial, retry. Hash/truncation/tamper → re-download or abort. Manifest replay/expiry → reject, keep highest trusted. Power-loss/crash/partial-replace/rename-fail → backup-based recovery. Skipping versions → direct old→latest. Rollout bucket → server-decided. Active user → defer non-mandatory.

---

## 9. SP5 — Dashboard (Agents page, additive; per-company)

Keep all existing UI. **Add:**
- **Distribution panel** per active release: rollout % (slider 0–100), mandatory toggle, channel select, min-supported-version, expiry. Saves via new installer PATCH fields. A **"Roll back"** action re-activates the previous version.
- **Fleet adoption / update status:** version-distribution bars (from `devices.agent_version`) + recent `update_runs` (status, failures, retry counts) from `GET /v1/updates/runs`.
- **Scope:** everything filtered to the admin's own `company_id` (D1/D2). No cross-company UI (no super-admin model exists; adding one would be non-additive).

---

## 10. CDN-readiness

A single helper `app/core/release_urls.py::download_url(s3_key)`: returns `f"{CDN_BASE_URL}/{s3_key}"` when `settings.CDN_BASE_URL` is set, else the current presigned-S3 URL. New config `CDN_BASE_URL: str = ""` (additive; blank = today's behavior). Manifest/`download_url`/public-download all route through it.

---

## 11. Deferred / Out of Scope
Delta/per-file updates; OS code-signing + notarization; separate launcher **binary**; CloudFront provisioning; full CI agent build; multi-region. The manifest schema (`files[]`, `schema:1`) and URL helper are forward-compatible so these can land later without breaking changes.

---

## 12. Security Requirements (mapped)
Signed manifest (Ed25519, embedded trust key) · sha256 enforced before install · HTTPS transport · reject unsigned/tampered/expired/older-than-trusted · staged files non-executable until verified · backups protected until success confirmed · device-JWT auth + company isolation on all new endpoints.

---

## 13. Testing Strategy
- **Backend (pytest via Docker):** semver compare; rollout bucketing determinism + ramp monotonicity; min-version forcing; expiry; manifest sign/verify round-trip + tamper rejection; `/check` backward-compat (old keys intact; defaults reproduce today); `/report` upsert + leniency; company isolation.
- **Agent (unit + manual):** manifest verify (good/tampered/expired/replayed); resumable download (truncate→resume; hash mismatch→re-download); disk-space abort; lock single-updater; atomic swap + rollback (simulate failed health check); interrupted-update recovery (kill mid-install); mandatory no-defer. Manual e2e on a macOS + Windows VM.
- **Dashboard:** rollout/mandatory/channel persistence; fleet view renders from real `update_runs`/`agent_version`; scope stays company-bound.

---

## 14. Backward-Compatibility Checklist
- [ ] Existing `installer_versions` rows unchanged; new columns nullable w/ today-equivalent defaults.
- [ ] `/v1/updates/check` keeps all current keys; new keys additive; default rollout=100 reproduces current outcomes.
- [ ] Existing `/v1/installers`, `/v1/downloads/*`, activate/delete/upload behavior unchanged.
- [ ] Agent `main.py`, `version.py`, identity, enrollment, autostart, watchers untouched; only `updater.py` internals enhanced.
- [ ] Build scripts (`build_macos.sh`, `build_windows.ps1`, `timetracker.spec`) unchanged; publish tool is separate.
- [ ] Old agents (no signature verification) keep updating via the original response keys.

---

## 15. Open Questions / Assumptions
- **A1 (assumption):** per-company scope only; no Edge super-admin cross-company screen (D1/D2). Revisit if cross-company management is later required (non-additive).
- **A2:** macOS atomic swap targets `/Applications/TimeTracker.app`; if the app runs from elsewhere for some installs, the updater resolves its own bundle path at runtime.
- **A3:** signing keypair lifecycle (rotation) uses `public_key_id` (`"v1"`); rotation = ship new public key in a new agent build + sign with new key (documented, not automated in v1).
