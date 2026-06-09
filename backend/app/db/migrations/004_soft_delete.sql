-- 004: add soft-delete flag to employees
ALTER TABLE employees ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_employees_is_deleted ON employees(is_deleted);
