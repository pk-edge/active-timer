-- Add Google OAuth sub (unique identifier from Google) to employees.
-- NULL for employees who have never signed in with Google.
--
-- The column add is a metadata-only change (no table rewrite, no index build),
-- so the brief ACCESS EXCLUSIVE lock it needs is sub-millisecond.
-- The UNIQUE guarantee comes from a separate CONCURRENTLY-built index instead
-- of an inline UNIQUE constraint: an inline constraint builds its index while
-- holding the exclusive lock, blocking every auth lookup on employees for the
-- duration. CONCURRENTLY takes no write-blocking lock (the migration runner
-- executes files containing CONCURRENTLY on an autocommit connection).
-- The DROP clears any INVALID leftover from a previously interrupted build.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS google_sub VARCHAR(255);
DROP INDEX CONCURRENTLY IF EXISTS ix_employees_google_sub;
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_employees_google_sub ON employees(google_sub)
