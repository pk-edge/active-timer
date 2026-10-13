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
| `backend/app/core/search.py` | Shared free-text search helper (LIKE-escaping, tokenized AND/OR matching, pg_trgm/unaccent relevance ranking) — see "Search" below |
| `backend/app/core/impersonation.py` | Super-Admin impersonation: the structural (un-grantable) eligibility test + the Redis `jti` session registry that makes revocation possible |
| `backend/app/core/scoping.py` | `visible_device_ids(user, db)` — resolves a user's device visibility (`all` / `own` / `groups`, with group-tree expansion) |
| `backend/app/core/ingest_queue.py` | In-memory bounded queue + 30s batched Postgres `COPY` flush for agent event ingestion |
| `backend/app/core/report_cache.py`, `tech_cache.py` | Background workers pre-aggregating company reports / tech-analytics into fast-lookup tables |
| `backend/app/services/ReportQueries.py` | Single home of the `fleet` / `device_full` report SQL + the shared active-time definition (`_ACTIVE_CASE`, `_EXCL_IDLE`) |
| `backend/app/core/analytics_cache.py` | Stale-while-revalidate + single-flight wrapper over `RedisCache` used by every Tech & AI endpoint |
| `backend/app/core/RedisClient.py`, `RedisCache.py` | Redis connection pool + stale-while-revalidate response cache (`tt:c:*`) |
| `backend/app/core/broadcaster.py` | Redis pub/sub SSE fan-out (`devices_updated`) across all backend replicas |
| `backend/app/core/AdminAuthCache.py` | In-process token→Employee cache for admin auth (short TTL) |
| `backend/app/core/device_auth_cache.py` | Redis device-context cache with tombstone-based invalidation |
| `backend/app/core/rate_limit.py` | `DistributedLimiter` — Redis fixed-window rate limiting w/ in-process fallback |
| `backend/app/core/RateLimitMiddleware.py`, `RequestSizeLimit.py` | Global per-user/device/IP/company rate limiting (429) and request-body caps (413) |
| `backend/app/core/LeaderLock.py` | Redis + flock leader election so singleton background loops run in one worker |
| `backend/app/core/DeviceStateSync.py` | Mirrors live per-device state across uvicorn workers via Redis |
| `backend/app/core/audit.py` | Background audit-event writer (off the request path), Redis stream-backed |
| `backend/app/core/device_janitor.py` | Marks devices offline after inactivity cutoff |
| `backend/app/core/update_signing.py`, `manifest.py`, `release_urls.py`, `rollout.py` | Agent auto-update release/manifest/signing pipeline (server side) |
| `backend/app/core/secrets.py` | Encrypted-at-rest Google OAuth credential loading (`SECRETS_KEY` + `secrets/*.enc`, or plain env) |
| `backend/app/db/models.py` | SQLAlchemy 2.0 async ORM models — see Cross-cutting: multi-tenancy |
| `backend/app/db/base.py` | Engine/session factories (two isolated pools: HTTP vs. ingest writer), migration bootstrap |
| `backend/app/db/migrations/` | Versioned, numbered raw-SQL migrations (34 as of this writing) |
| `backend/app/db/seed_rbac.py` | Seeds default roles/permissions |
| `backend/app/maintenance/idle_backfill.py` | Backfill job for idle-time gaps |
| `backend/app/scripts/` | Ops scripts: `seedAdmin`, `encryptSecrets`, `generateSecretsKey` |
| `dashboard/src/app/(auth)/` | Login/register route group (unauthenticated) |
| `dashboard/src/app/(dashboard)/` | All authenticated pages: home, overview, team, activity, categories, devices, logs, reports, tech, users, groups, roles, policies, audit, settings, files, agents |
| `dashboard/src/lib/api.ts` | Single fetch client: Bearer JWT from `localStorage`, per-navigation `AbortController` generation so route changes cancel in-flight GETs |
| `dashboard/src/hooks/` | `useAuth`, `useUser`, `useRealtimeStream` (SSE), `useDevicesPush`, `useDebouncedValue`, `useStartReportJob` (optimistic report-job start) |
| `dashboard/src/components/ExportReportDialog.tsx`, `lib/reportRange.ts` | Shared date-ranged report export dialog (Devices + Reports pages) and its range rules (UTC normalization, 732-day cap) |
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

**Group access follows role scope.** `employee_group_access` is only meaningful
for a `groups`-scoped role, and `users.py::_set_group_access` enforces that on
the way in ("Only users with a group-scoped role can be assigned group access").
Two things keep that invariant true, and a change to either side needs both:

- **Writing it**: the dashboard sends `group_ids: []` — not the previously
  ticked ids — whenever the selected role is not groups-scoped. Hiding the
  checkboxes is not enough; the state behind them survives a role change, and
  sending it produces a guaranteed 400.
- **Keeping it**: `roles.py` clears a role's holders' group access
  (`_clear_group_access_for_role`) when the role stops being groups-scoped or is
  deleted. Without this the rows are stranded — inert while the invariant is
  violated (scoping.py only reads them for a `groups`-scoped caller), but they
  RESURRECT if that user is ever moved back onto a groups-scoped role, silently
  restoring visibility nobody re-approved.

Migration 038 pruned the rows stranded before those two halves existed.

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
result.

- **One query module.** The `fleet` / `device_full` SQL lives only in
  `app/services/ReportQueries.py` (`build_fleet_report`, `build_device_full_report`);
  the background worker, the Redis-down sync fallback and the GET
  `/devices/fleet` + `/device/{id}/full` endpoints all call it. Pass `db=` to run
  sequentially on a request session; omit it (workers) to run the queries
  concurrently on private sessions, bounded process-wide by a semaphore so
  overlapping exports can't drain the connection pool. Long scans get a 300 s
  `statement_timeout`; job errors are logged, never echoed to the client.
- **Scope is resolved in the request handler** with `visible_device_ids()` and
  handed to the worker (which has no `admin`): `None` = all, list = only those,
  `[]` = nothing. This applies to `fleet` and `device_roster`; `device_full`
  checks the single device via `_get_visible_device`. The fleet Redis cache key
  carries a scope fingerprint (`fleet_cache_key`), so scoped and full-scope
  results never share an entry. `POST /jobs` returns a cache hit as an
  already-`done` job.
- **Range rules** (`_validate_job_range`, mirrored client-side in
  `lib/reportRange.ts`): both dates required, since ≤ until, until not after
  today (UTC), span ≤ `MAX_REPORT_RANGE_DAYS` (732 — "Last 2 Years" incl. a
  Feb 29).
- **One active-time definition.** `_ACTIVE_CASE` / `_EXCL_IDLE` are built from
  `report_cache._IDLE_APPS_SQL` (the list home.py also uses, macOS
  `loginwindow`/`ScreenSaverEngine` included), so cached, live, on-screen and
  exported totals agree.
- **UI.** Every date-ranged export goes through `ExportReportDialog`
  (`components/`), started via `useStartReportJob`. Devices page: row "Export"
  → `device_full` for that device; "Fleet Report" → `fleet`. Reports page:
  "Export report" pre-fills the page's current range and talent/device filter
  (device selected → `device_full`, none → scoped `fleet`).

The four quick-look report endpoints (`GET /company/summary`, `/company/top-apps`,
`/device/{id}/summary`, `/device/{id}/top-apps` — separate from the job queue
above) accept EITHER `days` OR an explicit `since`/`until` inclusive calendar
range (mutually exclusive, validated by `_validate_range()`). Both resolve
through `_resolve_window()` to the same thing — an inclusive UTC calendar-day
range; `days=N` means `[today-(N-1), today]`, exactly the DateRangePicker's
"Last N Days" — so a `days` request, the equivalent since/until and an export
of that range cover identical rows, and share a Redis response-cache key
(`_range_cache_fp` keys on the resolved dates). **Range cache (migration 039).** `report_cache` keeps three per-day tables
for up to 732 days: `report_daily_summary` (company/day),
`report_daily_device_cache` (company/day/device, active + idle) and
`report_daily_app_usage` (company/day/app). `_compute_day()` is the single
recompute unit (window + idle halves pre-aggregated per device into temp tables,
delete + re-insert under an advisory lock, idempotent). The fast cycle (10 min)
recomputes today + yesterday and stamps `report_cache_state.last_fast_refresh`.
Backfill walks `report_cache_state.covered_since` backwards a few days per cycle
(bounded time per cycle), lowering it only in the transaction that adds that
day to all three tables. Readers (`company_summary_range`, `device_summary_range`,
`company_top_apps_range`) answer only when `covers()` holds: state row exists,
`since >= covered_since`, and `last_fast_refresh` < 35 min old when the range
touches today/yesterday; otherwise `None` → live. Full-scope company summary and
top-apps, scoped and per-device summaries, and the fleet/device_full exports
all read these, so screen and export agree; scoped and per-device top-apps stay
live (few devices, covering index). `report_top_apps_cache` (old 7/14/30 windows)
is no longer read. Live fallbacks (`ReportQueries.daily_summary_sql` /
`top_apps_sql` / `top_devices_sql`) use the same split + per-(day, device)
pre-aggregation; `device_count` = devices with a window_change or idle_start
that day, in cache and live alike. **Late agent data** (agents buffer offline
and upload later): the leader recomputes days 2–7 back hourly and 8–30 back
daily, so data up to 7 days late shows within ~1 h, up to 30 days within ~24 h.
home.py's cache readers (`summary_from_cache`, `device_active_from_cache`) gate
freshness on `report_cache_state.last_fast_refresh` (no per-row touch-UPDATE).

**Heavy-query protection.** Live fallbacks of company summary / top-apps / fleet
are single-flighted: `try_acquire` returns a unique token, `release(key, token)`
is a token-checked delete, results/failure markers are written BEFORE release,
and losers (`wait_or_take`) roll back their session, then take over if the
winner's lock vanishes (e.g. client disconnect). Request-path computes run with
`LIVE_STATEMENT_TIMEOUT_MS` (60 s) inside an 80 s total budget (< 90 s lock
TTL); background jobs use 300 s on the background pool. Only a statement
timeout (sqlstate 57014) writes the 30 s failure marker → identical requests get
`503` + `Retry-After` without touching the DB; a pool-checkout timeout is a
plain `503` (Retry-After 5, no marker); other errors are 500s. since/until on
the quick-look GETs is capped at 732 days like the jobs. The Reports page
(`reports/page.tsx`) normalizes the picked range to the server's UTC "today"
(`normalizeReportRange`, shifting rather than clamping so "Last 7 Days" stays 7
days), sends plain `days` for ranges ending today and ≤90 days and since/until
otherwise, and uses that same normalized range as the export's initial range.

**Technology & AI filters**: the four Tech pages (`/tech`, `/tech/websites`,
`/tech/apps`, `/tech/category-usage`) narrow on the same four axes — date
range, group, talent, device — and every filterable endpoint in
`endpoints/tech_utilization.py` composes them the same way: `_scope_clause`
(visibility, always on) + `_group_clause` + `_employee_clause` +
`_device_clause`, concatenated into one `combined` SQL fragment. Three rules
follow from that shape and all three are load-bearing:

1. **The filter lookups are gated on `tech:view`, not on the module that owns
   the data.** `/v1/tech/filters/{groups,users,devices}` exist because
   `/v1/groups`, `/v1/users` and `/v1/devices` need their own `*:view`
   permissions and would 403 for a tech-only role, silently collapsing the
   page's own filters to nothing. Their visibility mirrors the pages':
   `visible_device_ids` for devices, group-tree expansion for the other two.
2. **A non-empty `combined` disables the `tech_cache` fast paths.** Each
   endpoint reads its pre-aggregated table only `if not combined` — the cache
   tables hold company-wide daily rollups with no per-group/user/device
   dimension, so a filtered request MUST fall through to live aggregation.
3. **Every filter value belongs in the Redis response-cache `_scope_fp`.**
   The fingerprint is `f"{admin.id}:{group_id}:{employee_id}:{device_id}"`;
   omitting one lets an unfiltered response be served to a filtered request.
   Adding a filter means touching the clause, the params dict, AND the
   fingerprint.

Tenant safety comes from the queries themselves (`d.company_id = :company_id`
is in every one) plus `_scope_clause`, so `_device_clause`/`_employee_clause`
need no ownership lookup — a foreign id intersects to zero rows. `_device_clause`
and `_group_clause` do validate UUID *shape*, because asyncpg raises rather than
simply not matching when a non-UUID is bound to a uuid column.

On the dashboard side the three list pages share `FilterBar` in
`components/UsagePageShell.tsx`; `/tech` renders its own chip row with the same
controls. Group, talent and device are mutually exclusive — selecting one
clears the other two, since all three narrow the same device set. The API
helpers in `lib/api.ts` take a `TechFilters` object rather than trailing
positionals.

**Why a filtered Tech query is fast (036).** Because rule 2 above sends every
filtered request to a live aggregation, the index that aggregation uses is the
whole performance story. `activity_events` carries TWO covering indexes on
purpose, and they are not redundant:

- `idx_events_window_covering (timestamp, device_id, app_name, duration)` —
  leads with `timestamp`, serves company-wide range scans (cache workers,
  home widgets, reports).
- `idx_events_window_device_covering (device_id, timestamp, app_name, duration)`
  — leads with `device_id`, serves every *filtered* Tech read.

A device-leading predicate cannot use the timestamp-leading index, and before
036 it fell back to `idx_events_device_ts`, which is not covering: one heap
fetch per row. Measured on 72M rows — 1 device over 30 days went 5,954 ms →
92 ms, and 50 devices over 30 days went 171,138 ms → 3,918 ms, because the plan
became an index-only scan. If you add a column to one of these queries, add it
to the index too or you silently reintroduce the heap fetches.

Both are partial (`WHERE event_type = 'window_change' AND app_name IS NOT NULL`)
and `window_change` is ~97.6% of rows, so each one costs an index update on
essentially every insert. Migration 028's checkpoint-chaining incident is the
reason that matters — read 028 and 031 before adding a third.

**Analytics response caching** lives in `app/core/analytics_cache.py`
(`cached_or_compute`), which every Tech endpoint goes through:

- *fresh* → serve; *stale* → serve immediately **and** refresh in a background
  task on its own `AsyncSessionLocal`; *miss* → compute.
- A Redis `SET NX` single-flight lock collapses concurrent misses, so the ten
  SWR calls a Tech page fires on mount compute once, not ten times.
- `ttl_for_range()` gives a range that ends before `tech_cache.RECENT_DAYS` a
  30 min / 6 h TTL instead of 2 min / 10 min — those rows are no longer being
  rescanned, so re-deriving "last month" every ten minutes was waste.
- The `/filters/*` lookups and `_productivity_map` are cached the same way;
  the category/rule mutation endpoints call `_evict_prod_map`.

**The constraint that makes this safe:** the `compute` closure must capture
plain values, never the request-scoped ORM `admin` — the background refresh runs
on a different session, where touching it raises `MissingGreenlet`. Every Tech
endpoint hoists `cid, company_uuid = str(admin.company_id), admin.company_id`
before defining `_compute` for exactly this reason. Failures degrade to
"compute it yourself": a Redis outage, a dead lock holder, or a refresh that
raises must never make a request fail, hang, or serve wrong data.

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

**Workers & leader election.** uvicorn runs `WEB_CONCURRENCY` workers (default
2, one per vCPU). `report_cache`, `tech_cache` and `device_janitor` are
singletons: `LeaderLock.LeaderElector("background")` starts them in exactly one
process (per-host `flock` + Redis `SET NX PX` lock renewed every 10 s,
token-checked release; Redis down → the flock holder leads, i.e. one per
container as with a single worker). `audit` and `ingest_queue` stay per-worker
(they drain per-process buffers). Live per-device state (`ingest_queue.device_cache`)
is mirrored across workers by `DeviceStateSync` (Redis hash `tt:dstate`).
Migrations/seed run under a Postgres advisory lock so two workers never apply
them concurrently. DB pools are per worker: HTTP 3+2 (checkout timeout 5 s) +
ingest 1+1 + background 1+1 (`BackgroundSessionLocal`: singleton loops and
report-job workers, so a backfill or exports never take HTTP connections) = 9
per worker, 18 for 2 workers, 36 during a rolling deploy (Postgres
`max_connections` 50). Every engine pins the session `timezone=UTC` (day
bucketing and `CURRENT_DATE` assume it). Singleton `start()`/`stop()` are
idempotent; if the leader lock file can't be opened the worker leads only while
holding the Redis lock. In-process auth caches are invalidated across workers by
`AuthInvalidationBus` (Redis channel `tt:auth_invalidate`; a listener reconnect
flushes both caches).

**Rate limiting & abuse protection** (pure-ASGI, SSE-safe, all numbers in
`config.py` `RATE_LIMIT_*`): `RateLimitMiddleware` charges every request to
Redis fixed-window buckets in one Lua round-trip, shared by all workers; Redis
down/slow → per-process counters (circuit breaker), never unlimited. Identity:
admin JWT subject (signature verified, no DB), device JWT subject for agent
traffic (own 1200/min bucket, never per-IP — offices sit behind NAT), client IP
otherwise (an expired token with a valid signature is keyed by its subject;
`*/stream` endpoints read `?token=`). Policies: burst 20/5 s, read 120/min, write
30/min, search (`?search=` / `?q=`) 30/min, `POST /v1/reports/jobs` 2/min,
company 6000/min (≈ the box's ~100 RPS burst ceiling — stops one tenant
monopolising the server; charged only if the user's buckets pass), anonymous
300/min/IP, Google OAuth its own per-IP bucket. Exempt: `/health*`, OPTIONS,
docs, and the credential POSTs (login/register/enroll/setup reset), which keep
their own limiters; SSE counts once at connect. Redis pool is a
`BlockingConnectionPool` (32/worker, 0.3 s wait); the limiter falls back to local
counters only after 3 consecutive Redis failures. Rejections are `429` + `Retry-After` +
`X-RateLimit-*`, with CORS headers (the middleware sits inside CORS). The
credential limiters in `rate_limit.py` (login/register/enroll/setup) are
separate and unchanged. `RequestSizeLimit` rejects bodies over 1 MiB with `413`
(declared or streamed), with per-route caps for ingest, CSV import, installer
and file uploads. Negative auth caches: a validly-signed device token whose
device is gone/revoked is remembered 60 s (`device_auth_cache`, cleared by
`invalidate_device()`), a missing/inactive admin 15 s (`AdminAuthCache`).
Client IP comes from `X-Forwarded-For` because `FORWARDED_ALLOW_IPS='*'`: correct
only while port 8000 is reachable solely through the ALB/proxy (ECS) — narrow it
for any directly exposed deployment. CORS exposes `Retry-After` and `X-RateLimit-*`. The
dashboard maps 429/503 to `RateLimitedError` / `ServiceBusyError` (`lib/api.ts`)
and SWR retries once after `Retry-After`, never immediately; the Devices SSE
reconnects with exponential backoff (paused while the tab is hidden) and report
job polling gives up after 10 consecutive failures. The Reports page never shows
a previous filter's data as current: each card has cold / refreshing (dimmed,
inert) / error (Retry) / empty / success states. Backend, Postgres and Redis
publish on `127.0.0.1` in compose (`BACKEND_BIND` to override).

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

**Search**: every backend free-text search param (`audit.py`'s `search`,
`users.py`'s `search` on both the list and impersonation-picker routes,
`adminFiles.py`'s `q`) goes through `app/core/search.py::build_search()` —
never hand-rolled `f"%{term}%"` ILIKE. It escapes `%`/`_`/backslash
(`escape_like()`, wildcard-injection-safe), tokenizes the query on whitespace
(AND across tokens, OR across the caller's columns — capped at 8 tokens),
folds accents and case via `unaccent(lower(...))`, and produces a 4-tier
relevance ORDER BY (exact > prefix > word-boundary > `pg_trgm` similarity,
floored at 0.3) callers append their own tiebreaker to. Backed by migration
`034_search_trgm.sql` (`pg_trgm` + `unaccent` extensions, GIN trigram indexes
on `employees(name/email)` and `devices(hostname)` — both extensions are on
the AWS RDS allowlist; ParadeDB/pg_search BM25 is not, hence trigram-based
ranking instead). Deliberately mirrors the dashboard's client-side
`smartMatch()`/`smartScore()`/`smartSort()` (`dashboard/src/lib/smartSearch.ts`)
semantics so a search behaves the same whether it runs server- or
client-side — `smartScore(...) > 0` is guaranteed equivalent to
`smartMatch(...)` by construction (the former short-circuits through the
latter), so switching a page from boolean filtering to ranked `smartSort`
never changes *which* rows match, only their order. `TalentSearchBar`
(`dashboard/src/components/TalentSearchBar.tsx`) is the shared "find a
person" combobox (keyboard nav, ARIA, two-line name+email/detail rows to
disambiguate people who share a name) — used on Reports, Activity Logs, and
(via `UsagePageShell`'s `FilterBar`) Website/App/Category Usage. Each caller
fetches and ranks its own candidate list (typically with `smartSort`) and
passes `results` in; the component holds no fetching logic of its own.

`audit.py`'s `search` param is the one exception to the trigram path above:
it's ranked with a hand-rolled Okapi BM25 (`app/core/search.py::build_bm25_search()`,
`Bm25Clause`), not `build_search()`. Audit content is document-like (dotted
action names, actor role, target type/id, a JSON metadata blob) where
whole-token frequency/IDF is the right relevance model — and unlike people/
hostname search, audit search does not need substring/typo tolerance, so
losing that is an acceptable, deliberate tradeoff here (ParadeDB/pg_search's
real BM25 index type isn't on the AWS RDS allowlist either, hence hand-rolled
on core tsvector/GIN — see `035_audit_bm25.sql`). Backed by a generated,
STORED `search_tsv` column on `audit_log` covering ONLY its own local columns
(`'simple'` config — no stemming, since action names/UUIDs/emails are
technical tokens) plus a GIN index. Per search request, `build_bm25_search()`
runs one extra company-scoped aggregate query for N/avgdl/n(t) (audit_log is
thousands of rows per company, not activity_events-scale, so this is an
acceptable per-request cost) and recovers per-document term frequency via
`unnest(tsvector)` (`length(tsvector)` only returns the *distinct* lexeme
count, not occurrences — verified against live Postgres 16). Deliberately
does NOT search the joined employee name/email; that stays on the trigram
path via other endpoints.

**`GET /v1/logs`**: paginated with `offset`/`limit` (not the analytics
endpoints' `days`-window style), ordered `(timestamp DESC, id DESC)` — the
`id` tiebreaker is required because many events share a timestamp, so
ordering by timestamp alone would duplicate or drop rows across pages.
Returns `{items, has_more, offset, limit}`, never an exact total: an
unbounded `COUNT(*)` over `activity_events` (tens of millions of rows) on
every page load isn't a cost this endpoint pays. `has_more` comes from a
cheap one-row over-fetch instead. `offset` is capped at 20,000 — on the
fleet-wide (no `device_id`) path it drives `_pagination_per_device`'s
per-device LATERAL sub-limit (capped at 1,000), so an uncapped `offset` would
be an uncapped per-device fan-out; deep fleet-wide pagination past that point
intentionally trades pagination completeness for bounded query cost (see the
docstring in `logs.py`). Narrowing with `device_id`/`employee_id` switches to
a flat, uncapped-depth query instead. The optional `search` param (also
`build_search()`-backed, see "Search" above) is applied server-side BEFORE
offset/limit pages through the result — not a client-side filter over
whatever page is already in hand — and, on the fleet-wide path, is folded
into the per-device LATERAL subquery's own WHERE (not applied after it), so
an older matching event on a busy device isn't discarded by the per-device
recency cap before search ever sees it. Only `build_search()`'s WHERE half is
used; the existing `timestamp DESC, id DESC` ordering is unchanged by
searching, since this is a chronological feed, not a relevance-ranked search.

**Realtime updates**: dashboard device/status changes fan out via Redis
pub/sub (`broadcaster.py`) to Server-Sent Events, consumed client-side by
`useRealtimeStream`/`useDevicesPush`.

## Local dev & deployment

Full walkthrough: `DEPLOY.md`. Summary:

| Service | Local URL | Image |
|---|---|---|
| Postgres | `127.0.0.1:15432` (`shared_buffers` 512MB, `max_connections` 50) | `postgres:16-alpine` |
| MinIO (S3 API / console) | `localhost:9002` / `localhost:9003` | `minio/minio` |
| Redis | `127.0.0.1:16379` (`requirepass`, 256 MB `volatile-lru`, FLUSHALL/FLUSHDB/KEYS/CONFIG/DEBUG disabled) | `redis:7-alpine` |
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
