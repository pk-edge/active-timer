-- 027_inactive_device_setting.sql
-- Adds per-company threshold for auto-archiving inactive devices.
-- NULL (default) = feature disabled; any positive integer = days of inactivity
-- before a device is automatically archived by the device_janitor worker.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS inactive_device_days INTEGER NULL;
