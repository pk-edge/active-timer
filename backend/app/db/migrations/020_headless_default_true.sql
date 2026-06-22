-- Agents are invisible by default. Admins can make them visible from the dashboard.
ALTER TABLE companies ALTER COLUMN agent_headless SET DEFAULT TRUE;
UPDATE companies SET agent_headless = TRUE WHERE agent_headless = FALSE;
