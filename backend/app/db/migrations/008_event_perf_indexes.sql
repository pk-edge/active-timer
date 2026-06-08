CREATE INDEX IF NOT EXISTS idx_events_device_type_ts ON activity_events(device_id, event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_device_ts ON activity_events(device_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON activity_events(event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_app_ts ON activity_events(app_name, timestamp DESC) WHERE event_type = 'window_change' AND app_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_app_category_rules_pattern ON app_category_rules(company_id, app_pattern);
