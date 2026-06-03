export interface Device {
  id:             string;
  hostname:       string;
  platform:       string;
  employee_name:  string | null;
  last_seen:      string | null;
  last_seen_unix: number | null;
  is_online:      boolean;
}

export interface TimelineEvent {
  id:             string;
  event_type:     string;
  app_name:       string | null;
  window_title:   string | null;
  url:            string | null;
  duration:       number;
  timestamp:      string;
  timestamp_unix: number;
}

export interface DailyStat {
  date:           string;
  active_seconds: number;
  idle_seconds:   number;
  total_seconds:  number;
}

export interface Screenshot {
  id:               string;
  s3_url:           string;
  captured_at:      string;
  captured_at_unix: number;
  file_size:        number;
}

export interface CompanyDay {
  date:           string;
  active_seconds: number;
  idle_seconds:   number;
  device_count:   number;
}

export interface AppUsage {
  app_name: string;
  seconds:  number;
}

export interface LogEvent {
  id:             string;
  event_type:     string;
  app_name:       string | null;
  window_title:   string | null;
  url:            string | null;
  duration:       number;
  timestamp:      string;
  timestamp_unix: number;
  device_id:      string;
  hostname:       string;
}
