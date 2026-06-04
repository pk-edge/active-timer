-- 005: notification read receipts
CREATE TABLE IF NOT EXISTS notification_reads (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id      UUID        NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    notification_key VARCHAR     NOT NULL,
    read_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (employee_id, notification_key)
);
CREATE INDEX IF NOT EXISTS idx_notif_reads_employee ON notification_reads(employee_id);
