-- 037_prune_retired_permissions.sql
--
-- Delete role_permissions rows for (module, action) pairs that no longer exist
-- in app/core/permissions.py.
--
-- bb0b17c removed the `screenshots` module from the catalog but shipped no
-- cleanup migration, leaving 42 Super Admin roles (one per company) holding a
-- `screenshots.view` row the code no longer knows about. That broke role
-- ASSIGNMENT outright: users.py::_assert_can_assign_role compares the caller's
-- permissions -- built in CODE, from all_permissions(), for a Super Admin --
-- against the target role's, read from the DATABASE. The orphan appeared only
-- on the role's side, so a Super Admin was told they could not grant
-- `screenshots.view` and therefore could not assign the Super Admin role at all.
--
-- The code-side guard is fixed too (retired pairs are skipped there now), so
-- this migration is about not carrying dead rows rather than about unblocking
-- the endpoint. Retired pairs grant nothing in any case: require() is only ever
-- called with pairs that exist in the catalog.
--
-- The VALUES list below is the catalog as of this migration. It is a snapshot
-- ON PURPOSE -- a migration must describe the world at the time it ran, not
-- re-derive it from whatever permissions.py says on some later boot, or
-- re-running history would delete permissions added after this point.
--
-- NOTE: never put a ';' inside a comment in these files -- the migration
-- runner splits statements on ';'.

DELETE FROM role_permissions
WHERE (module, action) NOT IN (
        ('activity','view'),
        ('agents','create'),
        ('agents','delete'),
        ('agents','edit'),
        ('agents','view'),
        ('audit','view'),
        ('categories','edit'),
        ('categories','view'),
        ('devices','archive'),
        ('devices','assign'),
        ('devices','delete'),
        ('devices','view'),
        ('files','create'),
        ('files','delete'),
        ('files','view'),
        ('groups','create'),
        ('groups','delete'),
        ('groups','edit'),
        ('groups','manage_members'),
        ('groups','view'),
        ('home','view'),
        ('logs','view'),
        ('overview','view'),
        ('policy','edit'),
        ('policy','view'),
        ('reports','view'),
        ('roles','create'),
        ('roles','delete'),
        ('roles','edit'),
        ('roles','view'),
        ('settings','edit'),
        ('settings','view'),
        ('team','view'),
        ('tech','edit'),
        ('tech','view'),
        ('users','create'),
        ('users','delete'),
        ('users','edit'),
        ('users','view')
);
