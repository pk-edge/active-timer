# TimeTracker (active-time) — Codebase Guide

Living structural reference. Read the relevant section before any non-trivial
change; update it in the same turn as any change that alters architecture,
adds/removes a module, or changes a cross-cutting pattern. Point-in-time
context (incidents, decisions, who/why/when) belongs in project memory, not here.

## Architecture at a glance

```
Laptop (Windows/macOS)                  Admin browser
   │                                        │
   ▼                                        ▼
Desktop Agent (Python/PySide6)      Dashboard (Next.js 15, App Router)
  - capture window/idle/URL               │
  - local SQLite queue (offline)          │  fetch (Bearer JWT in localStorage)
  - background auto-updater               │
   │  POST /v1/ingest (JWT)                │
   ▼                                        ▼
              Backend API (FastAPI, single monorepo image)
   ├─ PostgreSQL 16  (source of truth + pg-cache pre-aggregation tables)
   ├─ Redis 7        (SSE pub/sub, response cache, distributed rate limit)
   └─ MinIO/S3       (screenshots, presigned URLs)
```

Two build outputs ship separately from this daily dev loop: the desktop
**installer** (macOS `.dmg` / Windows `.msi`, PyInstaller-built, company
token + API base baked in at build time) and the **agent-simulator**
(headless load-test harness, standalone `docker-compose`, not part of the
main stack).

## Module / directory registry

| Path | Purpose |
|---|---|
| `agent/` | Desktop agent — single Python codebase for Windows + macOS, packaged via PyInstaller (`timetracker.spec`) |
| `agent/main.py` | Process entrypoint: wires DB, capture, sync, lock detection, updater, policy, config workers together |
| `agent/capture/` | `window_watcher`, `screenshotter`, `input_monitor`, `url_watcher` — the actual telemetry sources |
| `agent/sync/sync_worker.py` | Pushes queued events to `POST /v1/ingest`, exponential backoff offline |
| `agent/db/local_db.py` | SQLite (`queue.db`) offline event queue + local timer state |
| `agent/policy/policy_manager.py` | Enforces URL/category blocking policy pulled from backend |
| `agent/protection/` | Windows admin/guard hooks (tamper protection) |
| `agent/widget/floating_widget.py` | Qt floating timer UI |
| `agent/lock/` | Screen lock/unlock detection (pauses capture) |
| `agent/updater.py`, `agent/update_verify.py` | Auto-update client; verifies Ed25519-signed manifest (see `Desktop_Agent_Auto-Update_Architecture.md`) |
| `agent/enrollment.py`, `identity.py` | First-run enrollment via company token; persists `identity.json` (device id + JWT) |
| `backend/app/main.py` | FastAPI app: CORS (`*.onedge.co` regex + explicit list), startup config guard, lifespan-managed background services |
| `backend/app/api/v1/endpoints/` | One file per resource (auth, devices, users, roles, groups, policy, reports, tech_utilization, screenshots, ingest, agentConfig, googleAuth, installers, downloads, notifications, logs, audit, setup, company, adminFiles) |
| `backend/app/core/security.py` | Password hashing (bcrypt), JWT create/decode (device tokens + admin tokens, separate expiries) |
| `backend/app/core/permissions.py` | Single-source-of-truth RBAC module/action matrix, drives both API guards and dashboard UI |
| `backend/app/core/impersonation.py` | Super-Admin impersonation: the structural (un-grantable) eligibility test + the Redis `jti` session registry that makes revocation possible |
| `backend/app/core/scoping.py` | `visible_device_ids(user, db)` — resolves a user's device visibility (`all` / `own` / `groups`, with group-tree expansion) |
| `backend/app/core/ingest_queue.py` | In-memory bounded queue + 30s batched Postgres `COPY` flush for agent event ingestion |
| `backend/app/core/report_cache.py`, `tech_cache.py` | Background workers pre-aggregating company reports / tech-analytics into fast-lookup tables |
| `backend/app/core/RedisClient.py`, `RedisCache.py` | Redis connection pool + stale-while-revalidate response cache (`tt:c:*`) |
| `backend/app/core/broadcaster.py` | Redis pub/sub SSE fan-out (`devices_updated`) across all backend replicas |
| `backend/app/core/AdminAuthCache.py` | In-process token→Employee cache for admin auth (short TTL) |
| `backend/app/core/device_auth_cache.py` | Redis device-context cache with tombstone-based invalidation |
| `backend/app/core/rate_limit.py` | `DistributedLimiter` — Redis fixed-window rate limiting w/ in-process fallback |
| `backend/app/core/audit.py` | Background audit-event writer (off the request path), Redis stream-backed |
| `backend/app/core/device_janitor.py` | Marks devices offline after inactivity cutoff |
| `backend/app/core/update_signing.py`, `manifest.py`, `release_urls.py`, `rollout.py` | Agent auto-update release/manifest/signing pipeline (server side) |
| `backend/app/core/secrets.py` | Encrypted-at-rest Google OAuth credential loading (`SECRETS_KEY` + `secrets/*.enc`, or plain env) |
| `backend/app/db/models.py` | SQLAlchemy 2.0 async ORM models — see Cross-cutting: multi-tenancy |
| `backend/app/db/base.py` | Engine/session factories (two isolated pools: HTTP vs. ingest writer), migration bootstrap |
| `backend/app/db/migrations/` | Versioned, numbered raw-SQL migrations (32 as of this writing) |
| `backend/app/db/seed_rbac.py` | Seeds default roles/permissions |
| `backend/app/maintenance/idle_backfill.py` | Backfill job for idle-time gaps |
| `backend/app/scripts/` | Ops scripts: `seedAdmin`, `encryptSecrets`, `generateSecretsKey` |
| `dashboard/src/app/(auth)/` | Login/register route group (unauthenticated) |
| `dashboard/src/app/(dashboard)/` | All authenticated pages: home, overview, team, activity, categories, devices, logs, reports, tech, users, groups, roles, policies, audit, settings, files, agents |
| `dashboard/src/lib/api.ts` | Single fetch client: Bearer JWT from `localStorage`, per-navigation `AbortController` generation so route changes cancel in-flight GETs |
| `dashboard/src/hooks/` | `useAuth`, `useUser`, `useRealtimeStream` (SSE), `useDevicesPush`, `useDebouncedValue` |
| `dashboard/src/contexts/ReportJobsContext.tsx` | Async report-generation job tracking (client state) |
| `installer/macos/`, `installer/windows/` | Platform installer build scripts; company token + API base URL are baked in at build time (not runtime-configurable) |
| `agent-simulator/` | Standalone Dockerized load simulator (default 2000 simulated agents) — independent `docker-compose.yml`, does not touch the main stack |
| `DEPLOY.md` | End-to-end deployment/build/install walkthrough — read this before touching the release flow |
| `Desktop_Agent_Auto-Update_Architecture.md` | Full auto-update design (signed manifests, resumable downloads, atomic replace, rollback) — read before touching `updater.py`/`update_verify.py`/`backend/app/core/update_signing.py`/`manifest.py` |
| `bitbucket-pipelines.yml` | CI/CD: `staging` branch → staging ECR images + ECS force-deploy; `master` branch → prod. Dashboard `NEXT_PUBLIC_API_URL` is baked in at Docker build time per environment — this is the #1 way to break OAuth/API calls on a new environment if forgotten |

## Cross-cutting patterns

**Auth**: JWT (PyJWT, HS256) with two token kinds — device tokens
(`create_device_token`, 365-day expiry, used by the agent) and admin tokens
(`create_admin_token`, 12-hour expiry, used by the dashboard), both carrying
`company_id`. Passwords hashed with bcrypt. Google OAuth is an alternate admin
login path (`googleAuth.py`), gated by `ALLOWED_EMAIL_DOMAIN` and configured
via `backend/app/core/secrets.py`. The backend refuses to boot outside
`ENVIRONMENT=development` with a default/dev `JWT_SECRET` or wildcard CORS
(`_assert_secure_config` in `main.py`).

**Multi-tenancy / data isolation**: every tenant-scoped table carries a
`company_id` FK to `companies` (Employee, Device, Role, Group,
InstallerVersion, UpdateRun, AppCategory, AppCategoryRule, BlockedUrl,
SharedFile, …). Within a company, per-user device visibility is a second
layer resolved by `app/core/scoping.py::visible_device_ids` — `all` (company-wide,
admin), `own` (devices assigned to that employee), or `groups` (expanded group
tree). Always filter through this helper rather than re-deriving visibility.

**RBAC**: `app/core/permissions.py` is the single source of truth for the
(module, action) permission matrix; the dashboard fetches it live via
`GET /v1/roles/modules` so backend and UI never drift.

**RBAC anti-escalation, both directions**: `roles.py`'s `_assert_can_grant` /
`_assert_can_touch_system_role` stop a role-manager from *defining* a role with
more scope/permissions than they hold, or rewriting a system role, unless they
are themselves all-scope. `users.py`'s `_assert_can_assign_role` is the other
half — it stops the same caller from sidestepping that by *assigning* an
already-existing powerful role (including Super Admin) to a user via
`POST /v1/users`, `PATCH /v1/users/{id}`, or the approve flow. Both halves share
the same rule (`caller scope_type == "all"` unlocks system/all-scope roles; the
target role's permissions must be a subset of the caller's own) — a new
role-assignment surface needs both, not just one.

> **Every feature ships with its permission.** No endpoint or UI affordance
> lands without being wired into RBAC, in the same change that adds it:
> 1. the (module, action) pair exists in `app/core/permissions.py` — add the
>    module or action there first if the feature doesn't fit an existing one;
> 2. the endpoint is gated server-side with `Depends(require("<module>", "<action>"))`;
> 3. the UI hides or disables the trigger behind the matching `can("<module>", "<action>")`.
>
> Server-side gating alone is not "done": a button that 403s is a bug report
> waiting to happen. A feature reachable from a page whose module differs from
> the API it calls (a report generated from the Devices page, say) is gated on
> the module that *owns the API* — the Devices page checks `reports:view`
> before showing any report trigger.

**Super-Admin impersonation**: a Super Admin can operate the dashboard as any
active employee in their company. Core logic in `app/core/impersonation.py`;
endpoints `POST /v1/auth/impersonate`, `POST /v1/auth/impersonate/exit`, and the
picker feed `GET /v1/users/impersonatable`. Four things make it safe, and all four
are load-bearing:

1. **Un-delegable gate.** No `(module, action)` pair exists — eligibility is
   structural (`scope_type == "all" and is_system`) via `require_super_admin`, so
   it cannot be granted through the Roles UI and is invisible to everyone else.
   This is the documented exception to the permission-pair rule in `CLAUDE.md`.
2. **Token.** `create_impersonation_token()` mints `sub=<target>` with
   `imp/act_by/act_email/jti` and a 30-minute TTL (`IMPERSONATION_TTL_MINUTES`).
   `type` stays `"admin"` deliberately, so the session flows through the normal
   `get_current_user` and inherits the target's real permissions and
   `_scope_type` — there is no parallel auth path.
3. **Revocation.** This codebase otherwise has no token revocation at all. An
   `imp` token's `jti` is registered in Redis (`tt:imp:<jti>`) and checked on
   every request **for `imp` tokens only** — so ordinary auth gains no Redis hop
   and no new failure mode, while impersonation fails *closed* if Redis is down.
   `imp` tokens deliberately **bypass `AdminAuthCache`**, whose 15s token-keyed
   TTL would otherwise keep a revoked session alive. Exit revokes the `jti`, then
   re-mints a fresh admin token after re-verifying the admin — the admin's
   original token is never stored client-side.
4. **Blast radius.** `forbid_impersonation` blocks `/v1/auth/set-password`, and
   `PATCH /v1/users/{id}` refuses `password`/`role_id`/`is_active` changes mid-
   session, so impersonation can never become account takeover or an escalation
   route out of itself. Chaining is blocked by `require_super_admin` refusing an
   already-impersonated caller.

Audit: `audit_log.impersonator_id`/`impersonator_email` (migration 033) are
stamped by `audit.record()` for **every** action taken during a session, so all
existing call sites inherit attribution — the acting identity stays the target
(that is what happened) while the real driver is recorded and queryable.
Start/stop emit `user.impersonate.start` / `user.impersonate.stop`.

**Reports & exports**: three report types run through one Redis-backed job
queue (`POST /v1/reports/jobs`, polled via `GET /v1/reports/jobs/{id}`, all
behind `reports:view`): `fleet` and `device_full` (date-ranged activity
analytics) and `device_roster` (a point-in-time inventory of registered
devices — hostname, assigned user, platform, status — with no activity
metrics, so it takes no date range). Workers live in
`endpoints/reports.py`; each falls back to running synchronously inside the
request when Redis is down. Jobs carry only JSON — file rendering happens
client-side in `dashboard/src/lib/reportDownload.ts` (xlsx / jsPDF), tracked by
`ReportJobsContext` and surfaced in the sidebar's Downloads tray, where a
finished job stays until dismissed so it can be re-opened from its cached
result. Roster scope is resolved with `visible_device_ids()` in the request
handler and passed into the worker, which has no `admin` to re-derive it from.

**Background jobs & caching**: the backend runs several always-on background
tasks started from `main.py`'s `lifespan`: `RedisClient` (cache pool),
`broadcaster` (SSE pub/sub — must start before workers publish),
`audit` (async audit writer), `ingest_queue` (30s batched Postgres `COPY`
flush for agent events — `activity_events` is tens of millions of rows),
`report_cache` / `tech_cache` (pre-aggregation workers refreshing summary
tables on a schedule so the two heaviest dashboard endpoints hit a fast-path
table instead of a live `GROUP BY` scan), and `device_janitor` (marks devices
offline after `ONLINE_CUTOFF_SECS`). All cache layers are designed to fail
open to a live DB query if Redis is unavailable.

**Ingest pipeline**: agents `POST /v1/ingest` → 202 immediately, event lands
in an in-memory bounded `asyncio.Queue` (back-pressure 503 if full) → flushed
every 30s via `COPY` into a temp staging table → `INSERT … ON CONFLICT` dedup
into `activity_events` in one round-trip. Runs on a dedicated DB connection
pool (`ingest_engine`/`IngestSessionLocal`), isolated from the HTTP request
pool, so a slow ingest flush can never starve interactive API requests.

**Migrations**: numbered raw-SQL files in `backend/app/db/migrations/`,
tracked in a `schema_migrations` table so re-running `init_db()` on every
boot executes zero DDL once applied. Files needing `CREATE INDEX
CONCURRENTLY` run on an autocommit connection. The migration statement
splitter strips comment-only lines before splitting on `;` — never rely on a
literal `;` surviving inside a SQL comment.

**Realtime updates**: dashboard device/status changes fan out via Redis
pub/sub (`broadcaster.py`) to Server-Sent Events, consumed client-side by
`useRealtimeStream`/`useDevicesPush`.

## Local dev & deployment

Full walkthrough: `DEPLOY.md`. Summary:

| Service | Local URL | Image |
|---|---|---|
| Postgres | `localhost:15432` | `postgres:16-alpine` |
| MinIO (S3 API / console) | `localhost:9002` / `localhost:9003` | `minio/minio` |
| Redis | `localhost:16379` (password-protected via `requirepass`) | `redis:7-alpine` |
| Backend | `localhost:8000` | built from `backend/Dockerfile` |
| Dashboard | `localhost:3002` | built from `dashboard/Dockerfile` |

`docker compose up --build -d` starts everything. The `backend` service has
**no bind mount** — `Dockerfile` does `COPY . .` at build time, so any local
`.py` edit requires `docker compose build backend` before it's visible to
`docker compose run`. Backend tests must run in Docker (local Python 3.13
cannot build the pinned `asyncpg`/`pydantic` wheels; image is
`python:3.12-slim`): `docker compose run --rm --no-deps backend python -m pytest tests`.

CI (`bitbucket-pipelines.yml`): `staging` branch builds+pushes
`tracker-{backend,dashboard}-staging` images to ECR and force-deploys the
staging ECS services; `master` does the same for `*-prod`. Prod dashboard is
`https://tracker.onedge.co`, prod API `https://api.tracker.onedge.co` (staging
equivalents under `*.staging.onedge.co`). The dashboard's `NEXT_PUBLIC_API_URL`
is a Docker **build** arg, not a runtime env var — it must be passed per
environment or every API call (including OAuth) silently targets the wrong host.

## Project-specific conventions worth knowing

- **Never let an unknown agent event type 422 the ingest endpoint** — the
  agent's sync worker retries the oldest failed batch forever, so a batch
  rejection permanently jams that device. Unknown event types must be skipped
  per-event inside a successful response.
- **CancelOnDisconnectMiddleware** (`app/core/request_cancel.py`) cancels
  in-flight GET handlers when the client disconnects (except `/v1/home/stream`);
  the dashboard mirrors this by aborting in-flight GETs on route change
  (`api.ts` navigation-scoped `AbortController`).
- Raw-SQL date arithmetic against a bound parameter needs an explicit
  `CAST(:param AS integer)` — asyncpg otherwise infers the parameter type from
  the left-hand column and produces an `operator does not exist` error.
- Endpoints must not read stale ORM attributes off `current_user` after a
  cache-miss `db.rollback()` (expires all instances in the session) — mutate
  the current user via explicit `UPDATE` statements instead.
