-- 023_perf_indexes.sql
-- Indexes on hot filter / foreign-key columns that were unindexed and showed up
-- as sequential scans under load. All built CONCURRENTLY so they never take a
-- write-blocking lock (the migration runner detects CONCURRENTLY and runs each
-- on an autocommit connection while ingest keeps writing). IF NOT EXISTS makes
-- the whole file safely re-runnable.

-- devices: the realtime/_status_rows scan, the devices list, and the logs feed
-- all filter WHERE company_id = :c AND is_active = TRUE. This is the single
-- hottest device-table predicate (polled every few seconds per dashboard tab).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_devices_company_active
    ON devices (company_id, is_active);

-- devices.employee_id is an unindexed FK hit on every users-activity load
-- (WHERE employee_id = ANY(...)) and my-stats (WHERE employee_id = :id). Partial
-- on NOT NULL keeps it small (unassigned devices are excluded).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_devices_employee
    ON devices (employee_id)
    WHERE employee_id IS NOT NULL;

-- employee_group_access.employee_id is read on EVERY request from a groups-scoped
-- user (scoping resolution) and again in list_users / notifications.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ega_employee
    ON employee_group_access (employee_id);

-- group_members: scoping expands assigned groups → member employees via
-- WHERE group_id = ANY(...) AND member_type = 'employee'. And group detail pages
-- look up by group. Two complementary indexes.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_group_members_group
    ON group_members (group_id, member_type);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_group_members_employee
    ON group_members (employee_id)
    WHERE employee_id IS NOT NULL;

-- notification_reads.employee_id is read on every NotificationBell poll
-- (every 30s per admin tab) to subtract already-read keys.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_notification_reads_employee
    ON notification_reads (employee_id);

-- audit_log: the audit page lists most-recent rows for a company.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_company_created
    ON audit_log (company_id, created_at DESC);
