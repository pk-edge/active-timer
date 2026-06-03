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

// ── RBAC ──────────────────────────────────────────────────────────────────────

export interface Permission { module: string; action: string; }

export interface ModuleDef { key: string; label: string; actions: string[]; }

export interface Me {
  id:          string;
  name:        string;
  email:       string;
  role_name:   string | null;
  scope_type:  string;
  permissions: Permission[];
}

export interface Role {
  id:          string;
  name:        string;
  description: string | null;
  scope_type:  string;
  is_system:   boolean;
  permissions: Permission[];
}

export interface User {
  id:           string;
  name:         string;
  email:        string;
  role_id:      string | null;
  role_name:    string | null;
  scope_type:   string | null;
  is_active:    boolean;
  is_approved:  boolean;
  group_ids:    string[];
  device_count: number;
}

export interface Group {
  id:           string;
  name:         string;
  description:  string | null;
  member_count: number;
  created_at:   string | null;
}

export interface GroupMember {
  id:          string;
  member_type: string;            // 'employee' | 'device'
  employee_id: string | null;
  device_id:   string | null;
  label:       string;
}

export interface GroupDetail extends Group {
  members: GroupMember[];
}

export interface Installer {
  id:             string;
  platform:       string;         // 'macos' | 'windows'
  installer_type: string;         // 'dmg' | 'msi' | 'exe'
  version:        string;
  file_size:      number;
  sha256:         string | null;
  notes:          string | null;
  is_active:      boolean;
  uploaded_at:    string | null;
}

// ── Home dashboards ─────────────────────────────────────────────────────────

export interface OverviewStats {
  total_devices:            number;
  active_today:             number;
  status:                   { active: number; passive: number; inactive: number };
  avg_active_seconds_today: number;
  trend:                    { date: string; active_seconds: number }[];
}

export interface RealtimeDevice {
  device_id:      string;
  hostname:       string;
  platform:       string;
  employee_name:  string | null;
  status:         "active" | "passive" | "inactive";
  current_app:    string | null;
  last_seen_unix: number | null;
}

export interface WebUsage { host: string; visits: number; }
