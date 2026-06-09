CREATE TABLE IF NOT EXISTS roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    scope_type  TEXT NOT NULL DEFAULT 'all',
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_roles_company_name ON roles(company_id, name);
CREATE INDEX IF NOT EXISTS idx_roles_company ON roles(company_id);

CREATE TABLE IF NOT EXISTS role_permissions (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    module  TEXT NOT NULL,
    action  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_role_perm ON role_permissions(role_id, module, action);
CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role_id);

ALTER TABLE employees ADD COLUMN IF NOT EXISTS role_id UUID REFERENCES roles(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    created_by  UUID REFERENCES employees(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_groups_company_name ON groups(company_id, name);
CREATE INDEX IF NOT EXISTS idx_groups_company ON groups(company_id);

CREATE TABLE IF NOT EXISTS group_members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id    UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    member_type TEXT NOT NULL,
    employee_id UUID REFERENCES employees(id) ON DELETE CASCADE,
    device_id   UUID REFERENCES devices(id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_member_one_target CHECK ((member_type = 'employee' AND employee_id IS NOT NULL AND device_id IS NULL) OR (member_type = 'device' AND device_id IS NOT NULL AND employee_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_group_employee ON group_members(group_id, employee_id) WHERE employee_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_group_device ON group_members(group_id, device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);

CREATE TABLE IF NOT EXISTS employee_group_access (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id UUID NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    group_id    UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_ega ON employee_group_access(employee_id, group_id);
CREATE INDEX IF NOT EXISTS idx_ega_employee ON employee_group_access(employee_id);

CREATE TABLE IF NOT EXISTS installer_versions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id     UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    platform       TEXT NOT NULL,
    installer_type TEXT NOT NULL,
    version        TEXT NOT NULL,
    s3_key         TEXT NOT NULL,
    file_size      BIGINT NOT NULL DEFAULT 0,
    sha256         TEXT,
    notes          TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT FALSE,
    uploaded_by    UUID REFERENCES employees(id) ON DELETE SET NULL,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_installer_versions_company ON installer_versions(company_id, platform);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_installer ON installer_versions(company_id, platform) WHERE is_active;

CREATE TABLE IF NOT EXISTS audit_log (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        UUID,
    actor_employee_id UUID,
    action            TEXT NOT NULL,
    target_type       TEXT,
    target_id         TEXT,
    metadata          JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_company_time ON audit_log(company_id, created_at DESC);
