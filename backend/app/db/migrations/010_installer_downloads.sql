-- ── Installer download tracking ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS installer_downloads (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    installer_id     UUID NOT NULL REFERENCES installer_versions(id) ON DELETE CASCADE,
    downloaded_by    UUID NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    downloaded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_installer_downloads_installer ON installer_downloads(installer_id);
CREATE INDEX IF NOT EXISTS idx_installer_downloads_employee  ON installer_downloads(downloaded_by);
CREATE INDEX IF NOT EXISTS idx_installer_downloads_at        ON installer_downloads(downloaded_at DESC);
