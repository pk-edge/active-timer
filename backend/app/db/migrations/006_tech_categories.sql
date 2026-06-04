CREATE TABLE IF NOT EXISTS app_categories (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        VARCHAR NOT NULL,
    color       VARCHAR NOT NULL DEFAULT '#6366f1',
    description TEXT,
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app_category_rules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id  UUID NOT NULL REFERENCES app_categories(id) ON DELETE CASCADE,
    company_id   UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    app_pattern  VARCHAR NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (company_id, app_pattern)
);

CREATE INDEX IF NOT EXISTS idx_app_categories_company ON app_categories(company_id);
CREATE INDEX IF NOT EXISTS idx_app_category_rules_company ON app_category_rules(company_id);
CREATE INDEX IF NOT EXISTS idx_app_category_rules_category ON app_category_rules(category_id)
