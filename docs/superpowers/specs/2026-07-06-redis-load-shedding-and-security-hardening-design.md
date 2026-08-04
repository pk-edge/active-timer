# Redis Load-Shedding & Security Hardening — Design

**Date:** 2026-07-06
**Goal:** Push more request-path work off Postgres and onto Redis / in-process caches so the
backend scales with fleet size, and close the security gaps found in a full-surface review —
**without changing any feature behavior**.

## Context (what the investigation found)

1. **The entire optimization layer was disconnected at HEAD.** Commit `adb27ae` ("logo updated")
   accidentally committed a stale `backend/app/main.py`, wiping the lifespan wiring for:
   `RedisClient.startup()` (→ every `tt:c:` response-cache call was a no-op; all home/tech/report
   endpoints computed live from Postgres on every poll), `broadcaster.startup()` (→ SSE push dead,
   dashboards fell back to polling), `report_cache.start()` / `tech_cache.start()` (→ pg-cache
   tables went stale after 35 min; every request fell through to live aggregation over ~42M-row
   `activity_events`), `audit.start()` (→ audit INSERT+flush inline on every mutating request),
   `device_janitor.start()`, plus `ORJSONResponse`, gzip, cancel-on-disconnect, security headers,
   scoped CORS methods/headers, and the CORS-wildcard boot guard.
2. With that restored, the remaining hot paths that scale with fleet size are:
   `GET /v1/agent/config` (2 DB queries per device per ~60 s), the device-JWT
   `GET /v1/policy/blocked-urls` (2 DB queries per poll), the per-request admin auth query in
   `get_current_user`, and event-loop-blocking S3 presign loops in screenshots.
3. Security review: strong baseline (parameterized SQL everywhere, bcrypt off-loop, OAuth state
   HMAC, scoped object access, Ed25519-signed updates). Real gaps: setup bootstrap gate counts
   only legacy `role=="admin"` rows; role management allows privilege escalation; rate limiter is
   per-replica; enrollment-token brute force unlimited; API docs exposed in prod; embedded Google
   OAuth secret in source (deliberate, prod depends on it — rotate, don't remove).

## Changes

### A. Performance (Redis + cache load-shedding)

- **A1. Restore `main.py`** to the fully-wired version from `9daca97` (verbatim). ✅ Done.
- **A2. Device-context auth cache — Redis, keyed by device_id** (`deps.get_device_ctx` +
  `device_auth_cache.device_ctx_cache_key`): device JWT is signature-verified per request, then
  `(device_id, company_id, employee_id, headless_override)` is served from Redis
  (`('devctx', device_id)`, TTL 120 s) with a SHA-256 token fingerprint pinning the entry to the
  exact JWT that validated it. Keyed by device_id so one `DEL` evicts it on **all** replicas —
  `invalidate_device()` (now async) fires on archive / delete / re-enroll / reassign / headless
  change, preserving the "agent sees changes on its next ~60 s poll" promise. A miss falls
  through to one read on the request's lazy session (with Redis absent this is exactly the
  pre-cache behavior, which also keeps the test suite's shared-session fixtures working).
- **A3. `GET /v1/agent/config`**: company config row cached in Redis (`('agentcfg', cid)`,
  fresh 30 s / stale 120 s, SWR); per-device response assembled from cached company data +
  cached `headless_override`. `cache_delete` on company-settings PATCH and headless toggles.
  Steady state: **zero DB queries** per poll.
- **A4. Device `GET /v1/policy/blocked-urls`**: company's active rules cached in Redis
  (`('policyrules', cid)`, fresh 30 s / stale 300 s); per-device filtering (all/device/employee)
  done in Python — identical response payload. `cache_delete` on policy create/update/delete.
  Steady state: **zero DB queries** per poll. (Agents poll ~60 s, so ≤30 s propagation is within
  today's effective latency.)
- **A5. Admin auth cache** (`app/core/admin_auth_cache.py`): in-process token → detached
  `Employee` (with `_perms`/`_scope_type` already attached), **TTL 15 s**, epoch-guarded like the
  device cache. `get_current_user` warm path = JWT decode only, zero DB. Cache cleared on any
  user/role mutation (users.py, roles.py). Bounded staleness: a permission change or deactivation
  propagates in ≤15 s on other replicas (instant on the mutating one); JWT expiry is still
  enforced on every request. The cached instance is detached and read-only by existing convention
  (the expunge design already requires mutations to use explicit UPDATE).
- **A6. Screenshots presigning off the event loop**: `generate_presigned_url` moved to
  `asyncio.to_thread` for the device upload path and batched for the list endpoint (up to 200
  synchronous HMAC signings currently block the loop).
- **A7. Distributed rate limiter** (`app/core/rate_limit.py`): Redis fixed-window limiter
  (INCR + EXPIRE, keys `tt:rl:*`) sharing the brute-force budget across replicas, failing open to
  the existing in-process sliding-window limiter when Redis is unavailable. Same limits and
  failure-only counting as today.

### B. Security hardening (no feature changes)

- **B1.** (via A1) Security headers, scoped CORS methods/headers, CORS-wildcard boot guard. ✅
- **B2. Setup bootstrap gate** (`setup.py`): `_admin_count` now also counts active employees
  whose RBAC role has `scope_type='all'` — a deployment whose admins are RBAC-only can no longer
  report `admin_count == 0` and silently re-open unauthenticated admin reset. Strictly widens the
  count, so bootstrap still opens on genuinely fresh deployments.
- **B3. Role anti-escalation** (`roles.py`): a caller can only grant permissions they themselves
  hold; `scope_type='all'` requires the caller to have all-scope; editing or deleting a system
  role requires an all-scope caller. Super Admins are unaffected (they hold everything).
- **B4. JWT hardening** (`security.py`): `decode_token` requires the `exp` claim (all minted
  tokens have one; a forged/legacy token without expiry is now rejected).
- **B5. Enrollment brute-force limiter** (`auth.py`): failed enrollment attempts (invalid
  enrollment token) limited per IP (30 failures / 15 min). Successful enrollments are never
  limited, so mass rollouts behind one NAT are unaffected.
- **B6. Setup endpoint limiter** (`setup.py`): 5 attempts / 15 min per IP on
  `POST /v1/setup/reset-admin` (slows secret guessing; legit use is one-shot).
- **B7. API docs gating**: new setting `API_DOCS_ENABLED` (default: enabled only when
  `ENVIRONMENT=development`); `/docs`, `/redoc`, `/openapi.json` disabled in staging/prod unless
  explicitly re-enabled via env.
- **B8. Embedded Google OAuth fallback visibility** (`secrets.py`): WARNING log when the
  embedded in-source credentials are actually used outside development. The fallback itself is
  kept — production deliberately depends on it; rotation is recommended in the report.

### C. Explicitly NOT changed (flagged in report only)

Dashboard JWT in localStorage; admin JWT in SSE query string (EventSource limitation) and OAuth
redirect URL; legacy `role in ("admin","manager")` full-permission fallback; MinIO
`anonymous set download` in the dev compose; removal of the embedded Google secret (rotate it);
TrustedHost middleware; moving the ingest queue to Redis Streams; `--workers 1` (scale via
replicas per config.py's connection budget).

## Testing

Rebuild the backend image (`docker compose build backend` — no bind mount) and run the full
suite: `docker compose run --rm --no-deps backend python -m pytest tests`. Cache behavior fails
open when Redis is absent, so tests without a Redis service exercise the live-DB paths unchanged.
