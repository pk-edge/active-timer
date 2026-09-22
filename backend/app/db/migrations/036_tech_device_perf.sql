-- 036_tech_device_perf.sql
--
-- Device-leading COVERING indexes for the Technology & AI pages.
--
-- The Tech pages gained a device filter, and every filtered shape was doing a
-- bitmap heap scan: idx_events_device_ts (device_id, timestamp DESC) is not
-- covering, so reading app_name/duration/event_type costs one heap fetch per
-- row. Measured on a 72M-row / 22 GB activity_events:
--
--     1 device, 30 days      5,954 ms  ->    173 ms   (34x)
--    50 devices, 30 days   171,138 ms  ->  3,597 ms   (48x)
--
-- The same rows read index-only took 44 ms vs 5,954 ms, so the 135x gap was
-- pure heap I/O (33,806 random heap blocks for 34K rows). idx_events_window_covering
-- cannot help: it leads with timestamp, so a device_id = X predicate has to scan
-- the whole range. On the 50-device query the planner nested-looped over all
-- 2,003 company devices, which is where the 171 s came from.
--
-- WAL note (see 028 and 031). 028 dropped nine indexes after a 3.6:1
-- index-to-heap ratio caused continuous checkpoint chaining, and 031 re-added
-- exactly one. This adds one more to the hot insert path: window_change is
-- ~97.6% of rows, so this is 5 -> 6 index updates per event (~+25% index WAL).
-- Unlike the nine dropped in 028 -- every one of which had ZERO scans -- this
-- index serves the dominant filtered read shape on four pages. If checkpoint
-- chaining reappears, THIS IS THE FIRST INDEX TO DROP: the endpoints stay
-- correct without it, only slower.
--
-- Key columns rather than INCLUDE: both variants measured at exactly 4295 MB
-- on the same data, so the form matching idx_events_window_covering wins.
--
-- All statements use CONCURRENTLY so ingest writes are never blocked. The
-- migration runner detects CONCURRENTLY and runs the file on an autocommit
-- connection. IF NOT EXISTS / IF EXISTS keep the file re-runnable.
-- NOTE: never put a ';' inside a comment in these files -- the migration
-- runner splits statements on ';'.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_window_device_covering
    ON activity_events (device_id, "timestamp", app_name, duration)
    WHERE event_type = 'window_change' AND app_name IS NOT NULL;

-- The URL half of the same problem, at NET-ZERO index count: idx_events_urls is
-- already (device_id, timestamp DESC) WHERE event_type = 'url_visit' but omits
-- url itself, so Website Usage's device-filtered queries still heap-fetch. The
-- replacement keeps the identical predicate and merely adds url as a trailing
-- column, making it a strict superset -- no existing call site can regress.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_url_device_covering
    ON activity_events (device_id, "timestamp", url)
    WHERE event_type = 'url_visit';

DROP INDEX CONCURRENTLY IF EXISTS idx_events_urls;
