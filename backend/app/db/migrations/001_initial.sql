-- TimeTracker initial schema
-- Run once on a fresh RDS PostgreSQL instance.
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Companies ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS companies (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL,
    enrollment_token TEXT UNIQUE NOT NULL DEFAULT encode(gen_random_bytes(24), 'hex'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Employees ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS employees (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'employee',  -- employee | manager | admin
    password_hash TEXT,                              -- bcrypt, only admins/managers
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_employees_company ON employees(company_id);

-- ── Devices (laptops) ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS devices (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    employee_id   UUID REFERENCES employees(id) ON DELETE SET NULL,
    hostname      TEXT NOT NULL,
    platform      TEXT NOT NULL,
    agent_version TEXT,
    jwt_token     TEXT UNIQUE,
    enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_devices_company  ON devices(company_id);
CREATE INDEX IF NOT EXISTS idx_devices_employee ON devices(employee_id);
CREATE INDEX IF NOT EXISTS idx_devices_lastseen ON devices(last_seen);
-- Ensure one device per hostname per company (used for upsert during re-enrollment)
CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_company_host
    ON devices(company_id, hostname);

-- ── Activity events ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS activity_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,
    app_name     TEXT,
    window_title TEXT,
    url          TEXT,
    duration     INTEGER NOT NULL DEFAULT 0,
    timestamp    TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_device    ON activity_events(device_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON activity_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_type      ON activity_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_urls      ON activity_events(device_id, timestamp DESC)
    WHERE event_type = 'url_visit';

-- ── Screenshots ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS screenshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    s3_key       TEXT NOT NULL,
    s3_url       TEXT,
    captured_at  TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    file_size    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_screenshots_device ON screenshots(device_id);
CREATE INDEX IF NOT EXISTS idx_screenshots_time   ON screenshots(captured_at DESC);

-- ── Daily summaries (background job) ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS daily_summaries (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id        UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    date             DATE NOT NULL,
    active_seconds   INTEGER NOT NULL DEFAULT 0,
    idle_seconds     INTEGER NOT NULL DEFAULT 0,
    top_apps         JSONB,
    top_urls         JSONB,
    session_count    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (device_id, date)
);

CREATE INDEX IF NOT EXISTS idx_summaries_device ON daily_summaries(device_id, date DESC);
