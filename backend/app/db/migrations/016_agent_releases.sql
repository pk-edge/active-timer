-- 016_agent_releases.sql
-- Additive auto-update release intelligence + telemetry. Existing rows keep
-- today's behavior via defaults (rollout_percent=100, mandatory=false, stable).

ALTER TABLE installer_versions
    ADD COLUMN IF NOT EXISTS rollout_percent       INT          NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS mandatory             BOOLEAN      NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS channel               VARCHAR(20)  NOT NULL DEFAULT 'stable',
    ADD COLUMN IF NOT EXISTS min_supported_version VARCHAR(50),
    ADD COLUMN IF NOT EXISTS manifest_json         TEXT,
    ADD COLUMN IF NOT EXISTS manifest_sig          TEXT,
    ADD COLUMN IF NOT EXISTS expires_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS published_at          TIMESTAMPTZ;

-- NOTE rollout_percent is clamped to the 0..100 range in application code (the
-- installer PATCH endpoint and the release tool). A DB CHECK constraint is omitted
-- because the migration runner splits on the statement separator and cannot parse
-- dollar-quoted DO blocks. Comments here must avoid that separator char and quotes.

CREATE TABLE IF NOT EXISTS update_runs (
    id             UUID PRIMARY KEY,
    device_id      UUID NOT NULL REFERENCES devices(id)   ON DELETE CASCADE,
    company_id     UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    from_version   VARCHAR(50),
    target_version VARCHAR(50) NOT NULL,
    platform       VARCHAR(20),
    channel        VARCHAR(20),
    status         VARCHAR(40) NOT NULL,
    error_code     VARCHAR(100),
    progress       INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_update_runs_company_created ON update_runs (company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_update_runs_device         ON update_runs (device_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_update_run_device_target ON update_runs (device_id, target_version);
