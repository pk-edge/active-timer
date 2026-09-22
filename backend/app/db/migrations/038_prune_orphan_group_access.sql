-- 038_prune_orphan_group_access.sql
--
-- Delete employee_group_access rows whose holder is not on a groups-scoped role.
--
-- users.py::_set_group_access enforces the invariant "only a groups-scoped role
-- may hold group access", but nothing kept it true when the ROLE changed
-- underneath the user: roles.py could flip scope_type away from 'groups', or
-- delete a role (setting role_id = NULL on every holder), and neither touched
-- employee_group_access. Both gaps are now closed in roles.py
-- (_clear_group_access_for_role) -- this file clears the rows already stranded
-- by them.
--
-- These rows are inert for visibility while the invariant is violated, because
-- scoping.py only consults employee_group_access for a 'groups'-scoped caller.
-- The reason to remove them anyway is RESURRECTION: if such a user is later
-- moved back onto a groups-scoped role, their old assignments silently come
-- back instead of starting empty, granting visibility nobody re-approved.
--
-- Idempotent -- re-running deletes nothing once the set is empty.
--
-- NOTE: never put a ';' inside a comment in these files -- the migration
-- runner splits statements on ';'.

DELETE FROM employee_group_access ega
USING employees e
LEFT JOIN roles r ON r.id = e.role_id
WHERE ega.employee_id = e.id
  AND (r.id IS NULL OR r.scope_type IS DISTINCT FROM 'groups');
