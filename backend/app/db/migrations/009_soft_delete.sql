ALTER TABLE roles          ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE groups         ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app_categories ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE blocked_urls   ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_roles_not_deleted   ON roles(company_id)    WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_groups_not_deleted  ON groups(company_id)   WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_cats_not_deleted    ON app_categories(company_id) WHERE is_deleted = FALSE
