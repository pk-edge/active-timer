-- Composite index for the screenshots listing: WHERE device_id = ? ORDER BY captured_at DESC.
-- The existing single-column indexes (device_id) and (captured_at DESC) force a sort or a
-- much wider scan once a device has accumulated many screenshots.
--
-- CONCURRENTLY: built without blocking writes, so agents keep uploading during the build.
-- (The migration runner executes files containing CONCURRENTLY on an autocommit connection.)
-- The DROP clears any INVALID leftover from a previously interrupted concurrent build.
DROP INDEX CONCURRENTLY IF EXISTS idx_screenshots_device_time;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_screenshots_device_time ON screenshots(device_id, captured_at DESC)
