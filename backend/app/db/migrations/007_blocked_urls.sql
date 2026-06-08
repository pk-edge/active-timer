CREATE TABLE IF NOT EXISTS blocked_urls (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID         NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    pattern     VARCHAR      NOT NULL,
    target_type VARCHAR      NOT NULL DEFAULT 'all',
    device_id   UUID         REFERENCES devices(id)   ON DELETE CASCADE,
    employee_id UUID         REFERENCES employees(id) ON DELETE CASCADE,
    notes       TEXT,
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by  UUID         REFERENCES employees(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_blocked_urls_company   ON blocked_urls(company_id);
CREATE INDEX IF NOT EXISTS idx_blocked_urls_device    ON blocked_urls(device_id);
CREATE INDEX IF NOT EXISTS idx_blocked_urls_employee  ON blocked_urls(employee_id)
