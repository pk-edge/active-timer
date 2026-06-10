-- Backfill any missing permissions for Super Admin system roles.
-- Safe to run repeatedly — ON CONFLICT DO NOTHING is idempotent.
-- Covers the case where new modules (home, tech, categories, etc.) were added
-- to permissions.py after the Super Admin role was first created.
INSERT INTO role_permissions (role_id, module, action)
SELECT r.id, perms.module, perms.action
FROM roles r
CROSS JOIN (VALUES
    ('home',        'view'),
    ('tech',        'view'),
    ('tech',        'edit'),
    ('categories',  'view'),
    ('categories',  'edit'),
    ('policy',      'view'),
    ('policy',      'edit'),
    ('audit',       'view'),
    ('devices',     'view'),
    ('logs',        'view'),
    ('reports',     'view'),
    ('screenshots', 'view'),
    ('users',       'view'),
    ('users',       'create'),
    ('users',       'edit'),
    ('users',       'delete'),
    ('groups',      'view'),
    ('groups',      'create'),
    ('groups',      'edit'),
    ('groups',      'delete'),
    ('roles',       'view'),
    ('roles',       'create'),
    ('roles',       'edit'),
    ('roles',       'delete'),
    ('agents',      'view'),
    ('agents',      'create'),
    ('agents',      'edit'),
    ('agents',      'delete'),
    ('settings',    'view'),
    ('settings',    'edit')
) AS perms(module, action)
WHERE r.name = 'Super Admin'
  AND r.is_system = TRUE
  AND r.is_deleted = FALSE
ON CONFLICT (role_id, module, action) DO NOTHING;
