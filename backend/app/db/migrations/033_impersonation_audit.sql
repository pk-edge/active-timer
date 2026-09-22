-- Impersonation attribution on the audit trail.
--
-- During a Super-Admin impersonation session the acting identity IS the target
-- employee (that is the point of the feature), so actor_employee_id correctly
-- records them. These columns record WHO was really driving, so an action taken
-- under impersonation can never be mistaken for one the employee took
-- themselves. NULL for every ordinary action.
ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS impersonator_id    UUID,
    ADD COLUMN IF NOT EXISTS impersonator_email VARCHAR;

-- Partial index: impersonated actions are a tiny fraction of the table, and the
-- query that matters is "show me everything done under impersonation".
CREATE INDEX IF NOT EXISTS idx_audit_log_impersonator
    ON audit_log (impersonator_id, created_at DESC)
    WHERE impersonator_id IS NOT NULL;
