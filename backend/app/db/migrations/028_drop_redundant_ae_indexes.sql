-- 028_drop_redundant_ae_indexes.sql
--
-- activity_events had 14 indexes totalling 17 GB on a 4.7 GB heap (3.6:1 ratio).
-- Every ingest COPY of ~11 K events updated all 14 index pages, generating the
-- bulk of WAL that was causing continuous checkpoint chaining (800 MB/5-min cycle).
--
-- After profiling scan counts under live ingest + report-cache load, only 3
-- indexes were being used by the query planner:
--
--   activity_events_pkey        — PK lookups (3.2 M scans)
--   idx_events_device_ts        — device+timestamp (status checks, timelines)
--   idx_events_timestamp        — timestamp range (report-cache incremental scan)
--
-- The nine indexes below had ZERO scans.  For the dominant query pattern —
-- company-wide GROUP BY over 1,894+ device IDs via ANY() — the planner
-- correctly prefers a seq scan + hash aggregate over any index.  These indexes
-- provided no acceleration benefit while costing 13 GB of WAL on every INSERT.
--
-- Expected outcome: WAL generation drops ~75 % (14 → 5 index updates per row),
-- ending the checkpoint-chaining observed in production.
--
-- All DROPs use CONCURRENTLY so ingest writes are never blocked.  The migration
-- runner already executes files containing CONCURRENTLY on an autocommit
-- connection.  IF EXISTS makes the file safely re-runnable.

-- superseded by idx_events_device_ts (device_id, timestamp DESC)
DROP INDEX CONCURRENTLY IF EXISTS idx_events_device;

-- superseded by idx_events_type_ts (event_type, timestamp DESC)
-- which is itself below — kept only until a future query audit confirms
-- event_type-only predicates are extinct (see comment on type_ts below).
-- NOTE: never put a ';' inside a comment in these files — the migration
-- runner splits statements on ';'.
DROP INDEX CONCURRENTLY IF EXISTS idx_events_type;

-- (device_id, event_type, timestamp DESC) — 0 scans; planner uses seq scan for
-- company-wide queries and idx_events_device_ts for per-device queries.
DROP INDEX CONCURRENTLY IF EXISTS idx_events_device_type_ts;

-- (device_id, timestamp DESC) WHERE window_change AND app_name IS NOT NULL —
-- 0 scans; widgets endpoint scans 1,894 device IDs via ANY(), seq scan wins.
DROP INDEX CONCURRENTLY IF EXISTS idx_events_window_device_ts;

-- (timestamp DESC, device_id, duration) WHERE window_change AND app_name IS NOT NULL —
-- 0 scans; report-cache top-apps scans company-wide, seq scan wins.
DROP INDEX CONCURRENTLY IF EXISTS idx_events_window_ts_dev;

-- (event_type, timestamp DESC) — 0 scans; company-wide scans prefer seq scan.
DROP INDEX CONCURRENTLY IF EXISTS idx_events_type_ts;

-- (app_name, timestamp DESC) WHERE window_change AND app_name IS NOT NULL —
-- 0 scans; raw app_name is superseded by normalize_app_name functional index,
-- which is itself dropped below.
DROP INDEX CONCURRENTLY IF EXISTS idx_events_app_ts;

-- (normalize_app_name(app_name), timestamp DESC, device_id, duration)
-- WHERE window_change AND app_name IS NOT NULL — 0 scans; report-cache
-- top-apps GROUP BY prefers seq scan.
DROP INDEX CONCURRENTLY IF EXISTS idx_events_norm_app_ts_dev;

-- (normalize_app_name(app_name)) WHERE app_name IS NOT NULL — 0 scans;
-- superseded by idx_events_norm_app_ts_dev (now dropped above).
DROP INDEX CONCURRENTLY IF EXISTS idx_activity_events_app_name_normalized;
