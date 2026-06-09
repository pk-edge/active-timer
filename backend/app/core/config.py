import json
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://timetracker:timetracker@db:5432/timetracker"

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

    # ── Auto-seed (first-run) ─────────────────────────────────────────────────
    # Set all four to auto-create the company + first admin on startup.
    # Ignored if the company already exists (idempotent).
    SEED_COMPANY: str  = "Edge"
    SEED_EMAIL:   str  = ""
    SEED_NAME:    str  = "Admin"
    SEED_PASSWORD: str = ""

    # ── Remote setup endpoint ─────────────────────────────────────────────────
    # POST /v1/setup/reset-admin  (requires X-Setup-Secret header)
    # Leave blank to disable the endpoint entirely.
    SETUP_SECRET: str = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Default "*" allows any origin — restrict to your dashboard URL in production
    # via CORS_ORIGINS env var (comma-separated or JSON array).
    CORS_ORIGINS: List[str] = ["*"]

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
