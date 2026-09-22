-- 035_audit_bm25.sql
-- Hand-rolled BM25 ranking for the audit-log free-text search (app/api/v1/
-- endpoints/audit.py, backend/app/core/search.py::build_bm25_search()).
--
-- Why not the shared pg_trgm search module (034_search_trgm.sql)? Audit
-- content is document-like (dotted action names, actor role, target type/id,
-- a JSON metadata blob) where whole-token frequency + corpus-wide inverse
-- document frequency is the right relevance model — unlike people/hostname
-- search elsewhere, audit search does not need substring/typo tolerance.
-- ParadeDB/pg_search's true BM25 index type is not on the AWS RDS Postgres
-- allowlist, so this is built on core Postgres tsvector/GIN instead (see
-- CODEBASE_GUIDE.md "Search").
--
-- Generated, STORED tsvector column covering ONLY audit_log's own local
-- columns — NOT the joined employee name/email (those stay on the trigram
-- path elsewhere). `'simple'` config is deliberate: lowercase + tokenize
-- only, no stemming/stopword removal — action names like
-- `user.impersonate.start`, UUIDs, and emails embedded in metadata JSON are
-- technical tokens English stemming would mangle.
--
-- `metadata` is the actual column name (the ORM's `audit_metadata` attribute
-- maps to it via `mapped_column("metadata", JSON, ...)` in db/models.py).
--
-- GENERATED ALWAYS AS (to_tsvector(...)) STORED is the pattern PostgreSQL's
-- own docs use for full-text search generated columns; verified directly
-- against a live Postgres 16 instance before writing this (a json/jsonb
-- ::text cast inside a generated-column expression is immutable and allowed
-- here, no version-specific gotcha found).
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS search_tsv tsvector
  GENERATED ALWAYS AS (
    to_tsvector('simple',
      coalesce(action, '') || ' ' ||
      coalesce(actor_role, '') || ' ' ||
      coalesce(target_type, '') || ' ' ||
      coalesce(target_id, '') || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

-- CONCURRENTLY: audit_log is a live production table; a plain CREATE INDEX
-- would hold an exclusive lock for the whole build. The migration runner
-- (app/db/base.py::_apply_migration) detects the literal string
-- "CONCURRENTLY" and runs this entire file on an autocommit connection.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_log_search_tsv ON audit_log USING GIN (search_tsv);
