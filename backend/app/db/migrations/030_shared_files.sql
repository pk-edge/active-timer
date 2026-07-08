CREATE TABLE IF NOT EXISTS shared_files (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    uploaded_by       UUID        NOT NULL REFERENCES employees(id),
    original_filename TEXT        NOT NULL,
    s3_key            TEXT        NOT NULL,
    mime_type         TEXT,
    file_size_bytes   BIGINT      NOT NULL DEFAULT 0,
    public_token      TEXT        NOT NULL UNIQUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_shared_files_company_id    ON shared_files(company_id);
CREATE INDEX IF NOT EXISTS ix_shared_files_public_token  ON shared_files(public_token);
