-- 034_search_trgm.sql
-- Enables the shared search helper (backend/app/core/search.py): fuzzy
-- substring matching and trigram-similarity ranking via pg_trgm, plus
-- accent-insensitive matching via unaccent. Both extensions are on the AWS
-- RDS Postgres allowlist (ParadeDB/pg_search's true BM25 is not, so it is
-- not an option here).

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- GIN trigram indexes for the columns hit by the four search endpoints
-- (users list, impersonation picker, device lookups). CONCURRENTLY because
-- these are live production tables and a plain CREATE INDEX would hold an
-- exclusive lock for the whole build, blocking reads and agent ingest.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_employees_name_trgm
    ON employees USING GIN (name gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_employees_email_trgm
    ON employees USING GIN (email gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_devices_hostname_trgm
    ON devices USING GIN (hostname gin_trgm_ops);
