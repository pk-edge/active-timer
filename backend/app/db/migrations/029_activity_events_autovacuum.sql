-- 029_activity_events_autovacuum.sql
--
-- Tighten autovacuum thresholds for activity_events.
--
-- Default scale_factor=0.2 means autovacuum waits until 20% of a table's rows
-- are dead before vacuuming.  On a 37M-row table that is 7.4M dead tuples —
-- a threshold that may never be hit on an INSERT-heavy table with few DELETEs.
-- The result: the visibility map is never updated, forcing index-only scans to
-- fall back to heap fetches, and the free-space map drifts, causing page bloat.
--
-- Settings chosen:
--   vacuum_scale_factor  0.01  — vacuum when 1% rows dead    (~370K on 37M table)
--   vacuum_insert_scale_factor 0.05 — vacuum when 5% new inserts (~1.85M rows)
--   analyze_scale_factor 0.005 — analyze when 0.5% rows changed (~185K rows)
--
-- These are storage parameters on the table itself and take effect immediately
-- without restarting PostgreSQL or the application.

ALTER TABLE activity_events SET (
    autovacuum_vacuum_scale_factor        = 0.01,
    autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_analyze_scale_factor       = 0.005
);
