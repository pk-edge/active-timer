-- Additive: admin-controlled agent visibility. No rows touched, no data loss.

-- Company-wide default: make every agent in the company headless (invisible) or visible.
ALTER TABLE companies
  ADD COLUMN IF NOT EXISTS agent_headless BOOLEAN NOT NULL DEFAULT FALSE;

-- Per-device override: NULL = inherit the company default, TRUE/FALSE = force.
ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS headless_override BOOLEAN;
