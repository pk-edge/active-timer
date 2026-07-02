-- 026_tech_cache.sql
-- Pre-aggregated cache tables for the three heaviest Tech Utilization endpoints:
--   GET /v1/tech/utilization/summary            (3 full-table GROUP BY scans)
--   GET /v1/tech/utilization/category-allocation (2 scans — current + prev period)
--   GET /v1/tech/utilization/apps               (1 scan with LEFT JOINs to categories)
--
-- A background worker (app/core/tech_cache.py) refreshes all three tables every
-- 30 minutes. Full-scope admins with no group/employee filter read from here
-- instead of scanning activity_events directly. Filtered requests (group, employee,
-- category) still run live queries — the cache stores company-wide aggregates only.
--
-- All rows written in a single worker transaction share the same refreshed_at so
-- the stale threshold check (AND refreshed_at >= NOW() - N minutes) is always
-- all-or-nothing for a company — no partial-range cache poisoning.

-- ── Per-app daily aggregates ──────────────────────────────────────────────────
-- One row per (company, day, normalized-app-name). Drives:
--   • /utilization/apps  (aggregate over date range, then LEFT JOIN categories)
--   • /utilization/summary  total_seconds and total_apps
--   • top_app in summary
CREATE TABLE IF NOT EXISTS tech_daily_app_cache (
    company_id   UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    day          DATE        NOT NULL,
    app_name     TEXT        NOT NULL,   -- already normalize_app_name()-applied
    seconds      BIGINT      NOT NULL DEFAULT 0,
    user_count   INT         NOT NULL DEFAULT 0,  -- distinct employees that day
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, day, app_name)
);

CREATE INDEX IF NOT EXISTS idx_tech_daily_app_company_day
    ON tech_daily_app_cache (company_id, day DESC);

-- ── Per-category daily aggregates ─────────────────────────────────────────────
-- One row per (company, day, category-bucket). bucket_key = category_id::text
-- for named categories, 'uncategorized' for unclassified apps. Drives:
--   • /utilization/category-allocation  (current period + previous period)
--   • top_category in summary
CREATE TABLE IF NOT EXISTS tech_daily_category_cache (
    company_id    UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    day           DATE        NOT NULL,
    bucket_key    TEXT        NOT NULL,  -- category UUID or 'uncategorized'
    category_id   UUID,                  -- NULL for uncategorized rows
    category_name TEXT        NOT NULL,
    color         TEXT        NOT NULL DEFAULT '#94a3b8',
    seconds       BIGINT      NOT NULL DEFAULT 0,
    user_count    INT         NOT NULL DEFAULT 0,
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, day, bucket_key)
);

CREATE INDEX IF NOT EXISTS idx_tech_daily_cat_company_day
    ON tech_daily_category_cache (company_id, day DESC);

-- ── Daily totals per company ───────────────────────────────────────────────────
-- One row per (company, day) with company-wide headline numbers. Drives:
--   • total_users in /utilization/summary
CREATE TABLE IF NOT EXISTS tech_daily_totals_cache (
    company_id    UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    day           DATE        NOT NULL,
    total_seconds BIGINT      NOT NULL DEFAULT 0,
    total_apps    INT         NOT NULL DEFAULT 0,
    total_users   INT         NOT NULL DEFAULT 0,  -- distinct employees that day
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, day)
);

CREATE INDEX IF NOT EXISTS idx_tech_daily_totals_company_day
    ON tech_daily_totals_cache (company_id, day DESC);
