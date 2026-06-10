import json
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://timetracker:timetracker@db:5432/timetracker"

    # Connection pool — set per-environment to fit the DB's max_connections.
    # One task uses up to POOL_SIZE + MAX_OVERFLOW; a 2-task rolling deploy uses
    # 2x that, so keep (DB_POOL_SIZE + DB_MAX_OVERFLOW) * 2 < the DB max_connections.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 1800

    # Ingest queue worker ceiling — keep it under the pool headroom (pool minus a
    # few for HTTP requests) or a burst of workers re-exhausts the DB pool. Per-env.
    INGEST_MAX_WORKERS: int = 16

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

    # ── Online detection ──────────────────────────────────────────────────────
    ONLINE_CUTOFF_SECS: int = 60

    # ── Agent installer storage ─────────────────────────────────────────────────
    MAX_INSTALLER_MB: int = 600

    # ── Bootstrap admin ─────────────────────────────────────────────────────────
    # Idempotently created/repaired on startup from the tracker/admin-{env} secret,
    # so the admin login is seeded by deployment instead of a manual run-task.
    BOOTSTRAP_COMPANY: str = "Edge"
    BOOTSTRAP_ADMIN_EMAIL: str = ""
    BOOTSTRAP_ADMIN_NAME: str = "Admin"
    BOOTSTRAP_ADMIN_PASSWORD: str = ""

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
