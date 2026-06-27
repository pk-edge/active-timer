-- Groups can now be archived (reversible soft-hide) distinct from soft-delete.
-- Archived groups disappear from the main list but remain visible/manageable
-- from their parent's detail page.

ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_groups_archived ON groups(company_id, is_archived)
    WHERE is_deleted = FALSE AND is_archived = TRUE;
