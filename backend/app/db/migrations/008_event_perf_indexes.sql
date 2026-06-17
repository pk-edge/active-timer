-- Hot-path indexes for activity_events (reports, home, timelines) and category rules.
--
-- CONCURRENTLY: activity_events is large and under constant ingest writes in
-- production — a plain CREATE INDEX takes a SHARE lock that blocks ingest for
-- the whole build. The migration runner executes files containing CONCURRENTLY
-- on an autocommit connection (Postgres forbids them inside a transaction).
-- Each DROP clears any INVALID leftover from a previously interrupted build.
-- On databases where the index already exists and is valid, the drop+rebuild
-- runs once (the file is then recorded in schema_migrations and never re-run).
DROP INDEX CONCURRENTLY IF EXISTS idx_events_device_type_ts;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_device_type_ts ON activity_events(device_id, event_type, timestamp DESC);
DROP INDEX CONCURRENTLY IF EXISTS idx_events_device_ts;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_device_ts ON activity_events(device_id, timestamp DESC);
DROP INDEX CONCURRENTLY IF EXISTS idx_events_type_ts;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_type_ts ON activity_events(event_type, timestamp DESC);
DROP INDEX CONCURRENTLY IF EXISTS idx_events_app_ts;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_app_ts ON activity_events(app_name, timestamp DESC) WHERE event_type = 'window_change' AND app_name IS NOT NULL;
DROP INDEX CONCURRENTLY IF EXISTS idx_app_category_rules_pattern;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_app_category_rules_pattern ON app_category_rules(company_id, app_pattern);
