-- Nested groups: optional parent_id on groups.
-- Name uniqueness is scoped per parent (siblings cannot share a name).

ALTER TABLE groups ADD COLUMN IF NOT EXISTS parent_id UUID REFERENCES groups(id) ON DELETE RESTRICT;

DROP INDEX IF EXISTS uniq_groups_company_name;

CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id) WHERE parent_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_groups_company_parent_name
    ON groups (company_id, COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), name)
    WHERE is_deleted = FALSE;
