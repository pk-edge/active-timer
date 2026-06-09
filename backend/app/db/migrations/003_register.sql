ALTER TABLE employees ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT TRUE;
CREATE INDEX IF NOT EXISTS idx_employees_pending ON employees(company_id) WHERE is_approved = FALSE AND is_active = TRUE;
