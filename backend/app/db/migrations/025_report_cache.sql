-- 025_report_cache.sql
-- Pre-aggregated cache tables for the two slowest report endpoints:
--   GET /v1/reports/company/summary   (heavy GROUP BY + JOIN over activity_events)
--   GET /v1/reports/company/top-apps  (same, with normalize_app_name per row)
--
-- A background worker (app/core/report_cache.py) refreshes these tables every
-- 30 minutes. Endpoints for full-scope admins (scope='all') read from here
-- instead of scanning activity_events directly — simple PK lookups vs full
-- table aggregations. Scoped users (scope='own', scope='groups') still run
-- live queries since their result is a device-specific subset.

-- ── Daily activity summary per company ────────────────────────────────────────
-- One row per (company, calendar day). The worker upserts all companies in one
-- pass; endpoints query by company_id and a date-range window.
CREATE TABLE IF NOT EXISTS report_daily_summary (
    company_id     UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    day            DATE        NOT NULL,
    active_seconds BIGINT      NOT NULL DEFAULT 0,
    idle_seconds   BIGINT      NOT NULL DEFAULT 0,
    device_count   INT         NOT NULL DEFAULT 0,
    refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, day)
);

CREATE INDEX IF NOT EXISTS idx_report_daily_company_day
    ON report_daily_summary (company_id, day DESC);

-- ── Top apps per company per time window ──────────────────────────────────────
-- One row per (company, days_window, normalized_app_name).
-- days_window is one of 7 / 14 / 30 — the values the worker pre-computes.
-- The LIMIT applied at read time keeps fetches tiny.
CREATE TABLE IF NOT EXISTS report_top_apps_cache (
    company_id   UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    days_window  SMALLINT    NOT NULL,
    app_name     TEXT        NOT NULL,
    seconds      BIGINT      NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, days_window, app_name)
);

CREATE INDEX IF NOT EXISTS idx_report_apps_company_window_secs
    ON report_top_apps_cache (company_id, days_window, seconds DESC);
