-- 031_perf_and_settings.sql
--
-- 1) Partial COVERING indexes for the two hot aggregation shapes.
--
--    Dashboard + cache-worker queries aggregate window_change rows over a
--    timestamp range (today for widgets/overview, 1-day chunks for the cache
--    workers, up to 30 days for reports). Without a covering index each of
--    those is a bitmap heap scan: millions of scattered heap fetches on a
--    40M+-row table (measured 40-70 s per request). With ALL referenced
--    columns in the index the planner runs an INDEX-ONLY scan over a compact,
--    contiguous timestamp slice instead.
--
--    idx_events_window_covering serves (timestamp range + event_type =
--    window_change) GROUP BYs: home widgets, home overview, report_cache,
--    tech_cache, my-stats. normalize_app_name(app_name) is computed from the
--    app_name value read out of the index, so it stays index-only.
--
--    idx_events_idle_covering serves the idle_start half of the daily summary
--    (idle events are a small fraction of rows, so this index is tiny).
--
--    WAL note (see 028): this re-adds ONE index to the hot insert path,
--    chosen so every dashboard aggregation shares it. The nine dropped in 028
--    stay dropped.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_window_covering
    ON activity_events (timestamp, device_id, app_name, duration)
    WHERE event_type = 'window_change' AND app_name IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_idle_covering
    ON activity_events (timestamp, device_id, duration)
    WHERE event_type = 'idle_start';

-- 2) Per-host daily URL aggregates — powers /tech/websites and the URL half
--    of /tech/uncategorized from a tiny table instead of a url_visit scan.
--    Refreshed by app/core/tech_cache.py (day-chunked, incremental).
CREATE TABLE IF NOT EXISTS tech_daily_url_cache (
    company_id   UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    day          DATE        NOT NULL,
    host         TEXT        NOT NULL,   -- lowercased, www.-stripped
    seconds      BIGINT      NOT NULL DEFAULT 0,  -- LEAD()-estimated dwell time
    visits       BIGINT      NOT NULL DEFAULT 0,
    user_count   INT         NOT NULL DEFAULT 0,  -- distinct employees that day
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, day, host)
);

-- 3) Per-device daily active seconds — powers the home widgets / overview
--    "active today" + "avg active per device" numbers from a 2K-row lookup
--    instead of aggregating today's ~2M events per request. Refreshed by
--    app/core/report_cache.py on its fast cycle.
CREATE TABLE IF NOT EXISTS report_daily_device_cache (
    company_id     UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    day            DATE        NOT NULL,
    device_id      UUID        NOT NULL,
    active_seconds BIGINT      NOT NULL DEFAULT 0,
    refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, day, device_id)
);

-- 4) Free-form company settings for the dashboard Settings page (timezone,
--    work hours, tracking flags, notification prefs). JSONB so new settings
--    never need a migration.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb;
