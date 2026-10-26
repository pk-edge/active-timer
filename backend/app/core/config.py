import json
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://timetracker:timetracker@db:5432/timetracker"

    # Connection budget — TWO isolated pools PER WORKER PROCESS so background
    # ingest writes can never starve HTTP auth lookups (the "QueuePool limit
    # reached" jam):
    #   HTTP requests : DB_POOL_SIZE + DB_MAX_OVERFLOW               (default 3+2 = 5)
    #   ingest flusher: INGEST_DB_POOL_SIZE + INGEST_DB_MAX_OVERFLOW (default 1+1 = 2)
    #   background    : BACKGROUND_DB_POOL_SIZE + BACKGROUND_DB_MAX_OVERFLOW (default 1+1 = 2)
    # One worker therefore uses at most 7 connections.
    #
    # SINGLE-HOST SIZING (2 vCPU / 4 GB, Postgres max_connections=50, 2 workers):
    #   steady state  = WEB_CONCURRENCY(2) * 7 = 14 connections
    #   rolling deploy (old + new container both up)  = 28
    #   + migrations / psql / headroom                = comfortably < 50
    # The pool is deliberately small: with a 5 s checkout timeout a request
    # that cannot get a connection fails fast (503-ish) instead of convoying.
    # Two workers * 5 HTTP connections is plenty because the ingest hot path
    # never borrows from this pool (see the auth cache below).
    #
    # MULTI-REPLICA / RDS SIZING — READ BEFORE RAISING ANY OF THESE:
    #   Total Postgres connections = REPLICAS * WORKERS * (HTTP + ingest pool).
    #   Keep that (x2 mid rolling-deploy) well under max_connections. Scale by
    #   adding workers/replicas, NOT by inflating these pools — an oversized
    #   per-process pool is exactly what exhausts max_connections and takes the
    #   DB down. The ingest auth cache removes the per-post auth SELECT so you
    #   do NOT need a bigger HTTP pool to absorb 1000+ devices.
    DB_POOL_SIZE: int = 3
    DB_MAX_OVERFLOW: int = 2
    DB_POOL_TIMEOUT: int = 5    # fail fast instead of stacking 30s waits
    DB_POOL_RECYCLE: int = 1800
    # Session-level cap on any single statement from the HTTP pool (ms).
    # Protects the small pool from runaway analytics scans; background workers
    # override per-transaction with SET LOCAL, migrations reset it to 0.
    DB_STATEMENT_TIMEOUT_MS: int = 120_000

    # Dedicated ingest-writer pool (see budget above).
    # With the 30-second COPY strategy, only ONE connection is active at a time
    # (the flusher) — pool_size=1 is enough. Keep 1 overflow as a spare in case
    # the previous flush's connection is still being recycled when the next fires.
    INGEST_DB_POOL_SIZE: int = 1
    INGEST_DB_MAX_OVERFLOW: int = 1
    # Singleton background loops (report_cache / tech_cache / device_janitor)
    # and report-job workers get their OWN pool, so a multi-hour cache backfill
    # or two concurrent exports can never take the leader worker's HTTP
    # connections. Long scans set their own SET LOCAL statement_timeout.
    BACKGROUND_DB_POOL_SIZE: int = 1
    BACKGROUND_DB_MAX_OVERFLOW: int = 1

    # INGEST_MIN_WORKERS / INGEST_MAX_WORKERS are no longer used — the adaptive
    # worker pool has been replaced by a single timer-based COPY flusher.
    # The settings are kept here so existing deployments that set them via env
    # vars don't break on upgrade; they are silently ignored.
    INGEST_MIN_WORKERS: int = 1
    INGEST_MAX_WORKERS: int = 1

    # ── Ingest auth cache ──────────────────────────────────────────────────────
    # Caches (device token → device_id) in-process so the per-POST /v1/ingest auth
    # lookup does NOT borrow a connection from the HTTP pool on every 5s post.
    # This is the single biggest relief for pool contention at 1000+ devices: warm
    # tokens resolve from memory (zero DB), only cold/expired tokens do one short,
    # concurrency-bounded DB read. TTL bounds how long a revoked device keeps
    # validating (archive/delete/re-enroll also invalidate it immediately).
    # No Redis dependency — purely in-process, so it degrades to nothing if Redis
    # is unavailable. Raise TTL to cut DB auth reads further; lower it to tighten
    # revocation latency.
    INGEST_AUTH_CACHE_TTL_SECS: int = 60
    INGEST_AUTH_CACHE_MAX: int = 50_000
    # In-process cache for the per-request admin auth (user+permissions) query.
    # Deliberately short. invalidate_all() (every user/role mutation) clears the
    # mutating worker instantly AND is broadcast over Redis pub/sub
    # (app/core/AuthInvalidationBus.py) so sibling workers/replicas drop theirs
    # at once; the TTL is only the backstop for a missed message (Redis down). 0 disables the cache entirely (used by the test suite,
    # whose fixtures mutate rows with raw SQL that no invalidation hook sees).
    ADMIN_AUTH_CACHE_TTL_SECS: float = 15.0
    # Retry-After (seconds) returned when the ingest queue is saturated (503).
    # Matches the agent's SYNC_INTERVAL so it retries on its normal cadence.
    INGEST_RETRY_AFTER_SECS: int = 5
    # Max concurrent COLD auth lookups (cache misses) allowed onto the HTTP pool
    # at once. Bounds a cold-start stampede (e.g. 1500 agents reconnecting) so
    # miss traffic can NEVER exhaust the 10-slot HTTP pool — kept well below it.
    INGEST_AUTH_MISS_CONCURRENCY: int = 4
    # Negative auth caching: a validly-SIGNED device token whose device row is
    # missing/revoked/rotated-out is remembered for this many seconds so a
    # revoked agent retrying every 5 s cannot turn into a DB read per retry
    # (and so a flood of stale tokens cannot hammer the HTTP pool). Dropped
    # immediately by invalidate_device() (enroll / restore / archive / delete /
    # assign / headless), so a newly enrolled or restored device is never locked
    # out; cross-worker staleness is bounded by this TTL. 0 disables.
    INGEST_AUTH_NEGATIVE_TTL_SECS: float = 60.0
    INGEST_AUTH_NEGATIVE_MAX: int = 10_000
    # Same idea for admin tokens: a validly-signed token whose employee is
    # missing/inactive. Cleared by admin_auth_cache.invalidate_all() (every
    # user/role mutation). Disabled when ADMIN_AUTH_CACHE_TTL_SECS <= 0.
    ADMIN_AUTH_NEGATIVE_TTL_SECS: float = 15.0
    ADMIN_AUTH_NEGATIVE_MAX: int = 5_000

    # ── Logs feed fairness ──────────────────────────────────────────────────────
    # GET /v1/logs is a cross-device feed. A flat global LIMIT lets the busiest
    # devices monopolize the newest rows, so quieter-but-online devices vanish from
    # the page once the fleet is large (the "logs stop past ~100 devices" report).
    # We instead take the most-recent N events PER device (LATERAL, index-backed)
    # so every visible device is represented, then order+cap the merged feed.
    LOGS_PER_DEVICE_DEFAULT: int = 10

    # ── Runtime environment ────────────────────────────────────────────────────
    # "development" | "staging" | "production". Anything other than development
    # is treated as a real deployment and enforces secure-secret checks at boot.
    ENVIRONMENT: str = "development"

    # Expose /docs, /redoc and /openapi.json. Default (None) = only in
    # development; set API_DOCS_ENABLED=true to expose them on a real deployment.
    API_DOCS_ENABLED: bool | None = None

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_DEVICE_EXPIRE_DAYS: int = 365
    JWT_ADMIN_EXPIRE_HOURS: int = 12
    # Super-Admin impersonation sessions are deliberately far shorter than a normal
    # admin session: the token grants another person's identity, so it should expire
    # on its own well before anyone forgets they left it open.
    IMPERSONATION_TTL_MINUTES: int = 30

    # ── Activity sanity caps ────────────────────────────────────────────────────
    # Hard ceiling on a single window_change slice. The agent emits one slice per
    # ACTIVITY_HEARTBEAT_SECS (~60s), so a legitimate slice is tiny; anything far
    # larger means a sleeping/offline/old agent counted hours against one window
    # ("time keeps counting"). We clamp at ingest so the DB can never store an
    # impossible slice regardless of agent version. 300s is generous vs the
    # heartbeat yet kills hour-long slices.
    MAX_WINDOW_SLICE_SECS: int = 300

    # ── AWS / S3 ──────────────────────────────────────────────────────────────
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_BUCKET: str = "timetracker-screenshots"
    S3_ENDPOINT_URL: str = ""          # leave blank for real AWS; set for MinIO (internal)
    S3_PUBLIC_ENDPOINT_URL: str = ""   # external URL for presigned URLs given to agents (e.g. http://localhost:9000)
    PRESIGNED_URL_EXPIRY: int = 300    # seconds

    # ── Redis (SSE pub/sub) ─────────────────────────────────────────────────────
    # Backs app/core/broadcaster.py. Every backend process subscribes to one
    # channel here and fans incoming events out to its local SSE connections, so a
    # device-update published by ANY worker/replica reaches ALL dashboard tabs on
    # ALL replicas. Local dev default targets the compose `redis` service; staging
    # and production MUST point this at a managed instance (e.g. ElastiCache):
    #   REDIS_URL=redis://my-cluster.abc123.ng.0001.use1.cache.amazonaws.com:6379/0
    # Use rediss:// for TLS. If Redis is unreachable the API still serves and
    # ingests normally — only live SSE push degrades (clients fall back to polling).
    REDIS_URL: str = "redis://redis:6379/0"
    # Cache/limiter client pool (app/core/RedisClient.py), PER WORKER. A BLOCKING
    # pool: when all connections are busy a caller WAITS up to POOL_TIMEOUT_SECS
    # for one instead of failing instantly with "Too many connections" (which
    # used to trip the rate limiter's breaker under bursts). 2 workers x 32 is
    # tiny against Redis' default maxclients (10000). Keep the timeout short —
    # callers fail open to the DB / local limiter on any Redis error.
    REDIS_POOL_MAX_CONNECTIONS: int = 32
    REDIS_POOL_TIMEOUT_SECS: float = 0.3

    # ── Agent update signing ─────────────────────────────────────────────────
    # Base64 of a 32-byte Ed25519 seed. Blank in dev → a stable key is derived
    # from JWT_SECRET so signing works without provisioning. Set a real value in
    # production; the matching public key is embedded in the agent build.
    AGENT_SIGNING_PRIVATE_KEY: str = ""

    # Optional CDN in front of S3 for release files. Blank → presigned S3 URLs
    # (today's behavior). Set to a CDN origin (e.g. https://cdn.tracker.onedge.co)
    # to serve installers/manifests through the edge later — no code change.
    CDN_BASE_URL: str = ""

    # ── Online detection ──────────────────────────────────────────────────────
    # Must exceed the agent heartbeat (~20 s) + the 30 s ingest flush lag with
    # margin (docker-compose.yml pins the same 90 s).
    ONLINE_CUTOFF_SECS: int = 90

    # ── Agent installer storage ───────────────────────────────────────────────
    MAX_INSTALLER_MB: int = 600

    # ── Google OAuth ─────────────────────────────────────────────────────────
    # Credentials: loaded from secrets/google_credentials.enc when SECRETS_KEY is set,
    # otherwise read from GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars.
    GOOGLE_CLIENT_ID:     str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # Both auto-derive from the live request when blank, so one image works on
    # localhost, staging and prod. Set them only to pin an exact value.
    #   GOOGLE_REDIRECT_URI : blank → request.url_for(callback); whatever value
    #                         is used MUST be registered in Google Cloud Console.
    #   DASHBOARD_URL       : blank → the allowlisted origin the user signed in
    #                         from; used as the final fallback if that is absent.
    GOOGLE_REDIRECT_URI:  str = ""
    DASHBOARD_URL:        str = "http://localhost:3002"
    # Only this email domain is permitted to sign in via Google.
    ALLOWED_EMAIL_DOMAIN: str = "onedge.co"

    # ── Bootstrap admin ───────────────────────────────────────────────────────
    # Idempotently created/repaired on startup from the tracker/admin-{env} secret,
    # so the admin login is seeded by deployment instead of a manual run-task.
    BOOTSTRAP_COMPANY: str = "Edge"
    BOOTSTRAP_ADMIN_EMAIL: str = ""
    BOOTSTRAP_ADMIN_NAME: str = "Admin"
    BOOTSTRAP_ADMIN_PASSWORD: str = ""

    # ── Remote setup endpoint ─────────────────────────────────────────────────
    # POST /v1/setup/reset-admin  (requires X-Setup-Secret header)
    # Leave blank to disable the endpoint entirely.
    SETUP_SECRET: str = ""

    # ── Singleton background loops (app/core/LeaderLock.py) ───────────────────
    # report_cache / tech_cache / device_janitor run in exactly ONE worker (host
    # file lock + Redis lock). Failover after a hard kill takes <= TTL+INTERVAL.
    LEADER_LOCK_TTL_SECS: float = 30.0
    LEADER_LOCK_INTERVAL_SECS: float = 10.0

    # ── Global API rate limiting (app/core/RateLimitMiddleware.py) ─────────────
    # Pure-ASGI middleware, counters in Redis (fixed windows, shared by every
    # worker/replica) with an in-process fallback when Redis is down — it never
    # fails open to "unlimited". Identity: JWT subject when the bearer token
    # verifies, else the client IP. Credential endpoints (login/register/
    # enroll/setup) keep their own stricter limiters in app/core/rate_limit.py.
    # Disabled by the test suite (tests/conftest.py) — production default is ON.
    RATE_LIMIT_ENABLED: bool = True
    # Burst guard per user. The task brief suggested 10/5s, but one dashboard
    # page load fires ~8-10 parallel GETs (plus the SSE connect), so 10 would
    # trip on ordinary navigation; 20/5s still caps a runaway loop at 4 req/s.
    RATE_LIMIT_BURST_MAX: int = 20
    RATE_LIMIT_BURST_WINDOW_SECS: int = 5
    RATE_LIMIT_READ_PER_MIN: int = 120          # GET/HEAD, per user
    RATE_LIMIT_WRITE_PER_MIN: int = 30          # POST/PUT/PATCH/DELETE, per user
    RATE_LIMIT_SEARCH_PER_MIN: int = 30         # GET with ?search= / ?q=, per user (extra bucket)
    RATE_LIMIT_REPORT_JOB_PER_MIN: int = 2      # POST /v1/reports/jobs, per user (extra bucket)
    # Per-tenant ceiling over ALL admin (dashboard) users of one company. It exists
    # to stop one tenant monopolising the server, NOT to police users (per-user
    # buckets do that): 6000/min = ~100 RPS, the box's burst ceiling. ~150
    # active users with page loads + 15-60 s polling must never trip it.
    RATE_LIMIT_COMPANY_PER_MIN: int = 6000
    # Unauthenticated / forged-token traffic, per client IP. An office behind one
    # NAT shares this bucket, so it is sized for a whole office (300/min = 5 RPS).
    # Login/register/enroll/setup are exempt (own stricter limiters); a validly-
    # signed but EXPIRED token is bucketed by its subject, not by IP.
    RATE_LIMIT_ANON_PER_MIN: int = 300
    # Agent traffic (device JWT, any route): one bucket per DEVICE, never per
    # IP (many agents share an office NAT). A healthy agent makes ~15-25
    # requests/min (ingest every ~5s + config/policy polls); a backlog drain
    # after being offline posts full 100-event batches back-to-back
    # (~10-30 req/s, lossless — the agent keeps events until it sees a 2xx and
    # backs off on any non-2xx incl. 429). 1200/min leaves drain headroom while
    # still stopping a runaway/abusive token.
    RATE_LIMIT_DEVICE_PER_MIN: int = 1200
    # Redis circuit-breaker: after a Redis error the limiter uses its local
    # counters for this many seconds before probing Redis again (so a
    # black-holed Redis cannot add its socket timeout to every request).
    # It opens only after RATE_LIMIT_REDIS_FAIL_THRESHOLD CONSECUTIVE failures,
    # so one pool-exhaustion/timeout under a burst does not look like a dead server.
    RATE_LIMIT_REDIS_RETRY_SECS: float = 5.0
    RATE_LIMIT_REDIS_FAIL_THRESHOLD: int = 3

    # ── Request body caps (app/core/RequestSizeLimit.py) ─────────────────────
    # Default cap for every route; legitimately large upload routes get their
    # own caps below. Enforced from Content-Length AND by counting streamed
    # bytes (chunked / lying Content-Length). 0 disables the whole middleware.
    MAX_REQUEST_BODY_BYTES: int = 1 * 1024 * 1024
    # POST /v1/ingest: 100-event batches are ~100-300 KB; 4 MiB leaves room for
    # a larger SYNC_BATCH_SIZE in a future agent.
    MAX_INGEST_BODY_BYTES: int = 4 * 1024 * 1024
    # POST /v1/groups/import (CSV).
    MAX_IMPORT_BODY_BYTES: int = 5 * 1024 * 1024
    # POST /v1/files — company-configurable up to 5120 MB (company.py), so the
    # route cap is that ceiling plus multipart overhead. Installers use
    # MAX_INSTALLER_MB (+ overhead) automatically.
    MAX_FILE_UPLOAD_BODY_MB: int = 5121

    # ── CORS ──────────────────────────────────────────────────────────────────
    # *.onedge.co is permanently allowed via allow_origin_regex in main.py.
    # This list covers localhost dev origins; override via CORS_ORIGINS env var
    # (comma-separated or JSON array) for any other explicit origins.
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:3002",
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_cors(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
