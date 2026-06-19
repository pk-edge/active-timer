-- Additive: no rows touched, no data loss.

ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS is_deleted  BOOLEAN NOT NULL DEFAULT FALSE;

-- Grant archive and delete permissions to all existing Super Admin roles.
-- ON CONFLICT DO NOTHING is idempotent — safe to re-run.
INSERT INTO role_permissions (role_id, module, action)
SELECT r.id, perms.module, perms.action
FROM roles r
CROSS JOIN (VALUES
    ('devices', 'archive'),
    ('devices', 'delete')
) AS perms(module, action)
WHERE r.name = 'Super Admin'
  AND r.is_system = TRUE
  AND r.is_deleted = FALSE
ON CONFLICT (role_id, module, action) DO NOTHING;
