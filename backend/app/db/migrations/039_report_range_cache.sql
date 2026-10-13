-- 039_report_range_cache.sql
-- Per-day pre-aggregates that let the Reports page answer ANY picker range (up
-- to 732 days) from small tables instead of aggregating activity_events live.
--
-- Three tables hold one row set per (company, day): report_daily_summary
-- (company totals, exists since 025), report_daily_device_cache (per device,
-- gains idle_seconds here) and the new report_daily_app_usage (per normalized
-- app). app/core/report_cache.py fills all three together, day by day.
--
-- report_cache_state records the GLOBAL backfill horizon. Every day on or after
-- covered_since is fully computed for every company in all three tables, so a
-- reader may answer a range from the tables only when its start is on or after
-- that date. The worker extends the horizon backwards a few days per cycle and
-- the state row is absent until the first refresh, which makes every reader
-- fall back to the live query.

-- ── Per-company, per-day, per-app seconds ─────────────────────────────────────
-- app_name is normalize_app_name(raw), window_change only, idle/lock-screen
-- processes excluded (same definition as the live top-apps query).
CREATE TABLE IF NOT EXISTS report_daily_app_usage (
    company_id   UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    day          DATE        NOT NULL,
    app_name     TEXT        NOT NULL,
    seconds      BIGINT      NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, day, app_name)
);

-- ── Idle seconds per device-day ───────────────────────────────────────────────
-- Lets device-scoped and scoped-user summaries come from this table. A device
-- has a row for a day when it logged a window_change or an idle_start event, so
-- COUNT of rows is that day's device_count.
ALTER TABLE report_daily_device_cache
    ADD COLUMN IF NOT EXISTS idle_seconds BIGINT NOT NULL DEFAULT 0;

-- Scoped readers ask for a handful of device ids across a long range. The
-- primary key leads with (company_id, day) and cannot narrow by device, so this
-- index leads with device_id and carries every column the reader needs
-- (index-only scan).
CREATE INDEX IF NOT EXISTS idx_report_dev_cache_device_day
    ON report_daily_device_cache (device_id, day)
    INCLUDE (company_id, active_seconds, idle_seconds);

-- ── Global backfill horizon + liveness of the fast cycle ──────────────────────
-- One row, name = 'global'.
--   covered_since     every day >= this date is final or freshly computed
--   last_fast_refresh when the fast cycle (today + yesterday) last completed,
--                     readers refuse ranges that include those days when it is
--                     older than the staleness guard
CREATE TABLE IF NOT EXISTS report_cache_state (
    name              TEXT        PRIMARY KEY,
    covered_since     DATE,
    last_fast_refresh TIMESTAMPTZ
);
