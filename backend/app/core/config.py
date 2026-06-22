import json
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://timetracker:timetracker@db:5432/timetracker"

    # Connection budget — TWO isolated pools so background ingest writes can
    # never starve HTTP auth lookups (the "QueuePool limit reached" jam):
    #   HTTP requests : DB_POOL_SIZE + DB_MAX_OVERFLOW          (default 5+5 = 10)
    #   ingest writers: INGEST_DB_POOL_SIZE + INGEST_DB_MAX_OVERFLOW (default 2+2 = 4)
    # One task therefore holds at most 14 connections; a 2-task rolling deploy
    # spikes to 2x that (28), which fits even the smallest RDS/Postgres tiers
    # (max_connections >= 60). Raise per-environment only with the math above.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 10   # fail fast instead of stacking 30s waits
    DB_POOL_RECYCLE: int = 1800

    # Dedicated ingest-writer pool (see budget above).
    INGEST_DB_POOL_SIZE: int = 2
    INGEST_DB_MAX_OVERFLOW: int = 2

    # Ingest queue worker pool — bounded by its OWN connection pool so workers
    # can NEVER overflow it: more workers than connections just block on checkout
    # and time out. At startup MAX is hard-clamped to
    # (INGEST_DB_POOL_SIZE + INGEST_DB_MAX_OVERFLOW), and MIN is kept below MAX so
    # the pool has room to scale up under load and back down when idle.
    INGEST_MIN_WORKERS: int = 2
    INGEST_MAX_WORKERS: int = 4

    # ── Runtime environment ────────────────────────────────────────────────────
    # "development" | "staging" | "production". Anything other than development
    # is treated as a real deployment and enforces secure-secret checks at boot.
    ENVIRONMENT: str = "development"

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_DEVICE_EXPIRE_DAYS: int = 365
    JWT_ADMIN_EXPIRE_HOURS: int = 12

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
    ONLINE_CUTOFF_SECS: int = 60

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
