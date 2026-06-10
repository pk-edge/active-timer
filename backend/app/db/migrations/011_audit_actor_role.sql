-- Add actor_role to audit_log so the role at the time of action is preserved
-- even if the role name or assignment changes later.
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS actor_role TEXT;

-- Index for actor-based filtering
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(company_id, actor_employee_id);

-- Index for action filtering
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(company_id, action);

-- Index for target_type filtering
CREATE INDEX IF NOT EXISTS idx_audit_target_type ON audit_log(company_id, target_type);
