-- 024_overview_perf_index.sql
-- Partial index for the /home/overview hot path.
--
-- The overview endpoint now issues a single grouped scan:
--   WHERE event_type = 'window_change' AND app_name IS NOT NULL
--     AND timestamp >= NOW() - make_interval(days => N)
--   GROUP BY DATE(timestamp), device_id
--
-- The existing idx_events_device_type_ts covers (device_id, event_type, timestamp),
-- which the planner can use via a loop join after resolving company devices.
-- This smaller PARTIAL index covers the same rows with a narrower predicate,
-- giving a faster index-only scan for the (device_id, timestamp) group-by without
-- carrying the event_type column (already filtered). CONCURRENTLY so ingest
-- writes are never blocked during the build.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_window_device_ts
    ON activity_events (device_id, timestamp DESC)
    WHERE event_type = 'window_change' AND app_name IS NOT NULL;
