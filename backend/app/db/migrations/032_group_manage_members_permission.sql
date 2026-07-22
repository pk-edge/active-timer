-- Backfill groups.manage_members for every role that already holds groups.edit,
-- so existing roles don't lose the ability to add/remove group members now that
-- add/remove is split into its own permission. Safe to run repeatedly.
INSERT INTO role_permissions (role_id, module, action)
SELECT rp.role_id, 'groups', 'manage_members'
FROM role_permissions rp
WHERE rp.module = 'groups' AND rp.action = 'edit'
ON CONFLICT (role_id, module, action) DO NOTHING;
