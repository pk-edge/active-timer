export interface Device {
  id:             string;
  hostname:       string;
  platform:       string;
  employee_id:    string | null;
  employee_name:  string | null;
  last_seen:      string | null;
  last_seen_unix: number | null;
  is_online:      boolean;
  is_archived:    boolean;
  is_deleted:     boolean;
  headless?:          boolean;        // effective: what the agent applies
  headless_override?: boolean | null; // raw per-device override (null = inherit)
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

export interface MeetingSession {
  device_id:     string;
  platform:      string;   // "Zoom" | "Microsoft Teams" | "Google Meet" | "Slack huddle" | ...
  title:         string;
  start_ts:      number;   // unix seconds
  end_ts:        number;   // unix seconds
  ongoing:       boolean;
  active_secs:   number;   // live activity during the meeting (other apps included)
  duration_secs: number;   // wall-clock span
  apps:          AppUsage[];
}

export interface MeetingsResponse {
  total_active_secs: number;
  count:             number;
  sessions:          MeetingSession[];
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
  employee_id:    string | null;
  employee_name:  string | null;
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

export interface UserActivity {
  id:            string;
  name:          string;
  email:         string;
  role_name:     string | null;
  is_active:     boolean;
  device_count:  number;
  is_online:     boolean;
  last_seen:     string | null;
  current_app:   string | null;
  today_seconds: number;
  week_seconds:  number;
}

export interface UserDeviceActivity {
  id:            string;
  hostname:      string;
  platform:      string;
  is_online:     boolean;
  last_seen:     string | null;
  current_app:   string | null;
  today_seconds: number;
  week_seconds:  number;
}

export interface UserDay {
  date:           string;
  active_seconds: number;
  idle_seconds:   number;
}

export interface UserApp {
  app_name: string;
  seconds:  number;
}

export interface UserActivityDetail {
  user:     UserActivity;
  devices:  UserDeviceActivity[];
  daily:    UserDay[];
  top_apps: UserApp[];
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
  // Distribution config (auto-update rollout).
  rollout_percent?:       number;
  mandatory?:             boolean;
  channel?:               string;
  min_supported_version?: string | null;
  expires_at?:            string | null;
}

export interface UpdateRun {
  id:             string;
  device_id:      string;
  from_version:   string | null;
  target_version: string;
  status:         string;
  error_code:     string | null;
  progress:       number;
  updated_at:     string | null;
}

export interface UpdateRunsResponse {
  runs:     UpdateRun[];
  adoption: Record<string, number>;   // agent_version → device count
}

export interface InstallerDownload {
  id:                  string;
  installer_id:        string;
  platform:            string;
  version:             string;
  installer_type:      string;
  downloaded_by_id:    string;
  downloaded_by_name:  string;
  downloaded_by_email: string;
  downloaded_at:       string;
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

export interface MyStatsEvent {
  event_type:   string;
  app_name:     string | null;
  window_title: string | null;
  url:          string | null;
  duration:     number;
  timestamp_unix: number | null;
}

export interface Notification {
  key:        string;
  type:       string;
  title:      string;
  body:       string;
  created_at: string | null;
  is_read:    boolean;
  metadata:   Record<string, string>;
}

export interface MyStats {
  active_seconds_today: number;
  top_apps:             AppUsage[];
  recent_events:        MyStatsEvent[];
  device_count:         number;
}

// ── Technology & AI Usage ─────────────────────────────────────────────────────

export interface CategoryRule {
  id:          string;
  app_pattern: string;
  created_at:  string;
}

export interface AppCategory {
  id:          string;
  name:        string;
  color:       string;
  description: string | null;
  is_system:   boolean;
  rule_count:  number;
  rules:       CategoryRule[];
}

export interface TechSummary {
  total_hours:        number;
  total_apps:         number;
  total_users:        number;
  top_category:       string | null;
  top_category_color: string;
  top_app:            string | null;
}

export interface CategoryAllocationItem {
  category_id:   string | null;
  category_name: string;
  color:         string;
  seconds:       number;
  pct:           number;
  prev_seconds:  number;
  prev_pct:      number;
  pct_change:    number;
  hours:         number;
  user_change:   number;
}

export interface AppTrendItem {
  app_name:      string;
  category_name: string;
  color:         string;
  seconds:       number;
  prev_seconds:  number;
  pct:           number;
  prev_pct:      number;
  pct_change:    number;
  hours:         number;
  user_change:   number;
}

export interface AppTrends {
  trending_up:   AppTrendItem[];
  trending_down: AppTrendItem[];
}

export interface AppDetailItem {
  app_name:      string;
  category_id:   string | null;
  category_name: string;
  color:         string;
  seconds:       number;
  hours:         number;
  pct:           number;
  user_count:    number;
}

export interface AdoptionData {
  apps:   string[];
  series: Record<string, number | string>[];
}

export interface UncategorizedItem {
  pattern:    string;
  kind:       "app" | "url";
  seconds:    number;
  user_count: number;
  last_seen:  string | null;
}

export interface UncategorizedData {
  apps: UncategorizedItem[];
  urls: UncategorizedItem[];
}

// ── Website Usage ─────────────────────────────────────────────────────────────

export type Productivity = "productive" | "unproductive" | "neutral";

export interface WebsiteItem {
  host:          string;
  duration_secs: number;
  visits:        number;
  user_count:    number;
  pct:           number;
  productivity:  Productivity;
  category_name: string | null;
  color:         string;
}

export interface WebsiteListOut {
  items:        WebsiteItem[];
  total_secs:   number;
  total_visits: number;
  total_users:  number;
}

export interface WebsiteDetailTitle   { title: string; visits: number; pct: number; }
export interface WebsiteDetailSubpage { path: string;  visits: number; pct: number; }
export interface WebsiteDetailUser    { name: string;  visits: number; duration_secs: number; pct: number; }
export interface WebsiteDetailUsage   { date: string;  visits: number; }

export interface WebsiteDetailOut {
  host:     string;
  titles:   WebsiteDetailTitle[];
  subpages: WebsiteDetailSubpage[];
  users:    WebsiteDetailUser[];
  usage:    WebsiteDetailUsage[];
}

// ── Application Usage ─────────────────────────────────────────────────────────

export interface AppUsageItem {
  app_name:      string;
  duration_secs: number;
  user_count:    number;
  pct:           number;
  productivity:  Productivity;
  category_name: string | null;
  color:         string;
}

export interface AppUsageListOut {
  items:       AppUsageItem[];
  total_secs:  number;
  total_apps:  number;
  total_users: number;
}

export interface AppDetailTitle { title: string; duration_secs: number; pct: number; }
export interface AppDetailUser  { name: string;  duration_secs: number; pct: number; }
export interface AppDetailUsage { date: string;  duration_secs: number; }

export interface AppUsageDetailOut {
  app_name: string;
  titles:   AppDetailTitle[];
  users:    AppDetailUser[];
  usage:    AppDetailUsage[];
}

// ── Category Usage ────────────────────────────────────────────────────────────

export interface CategoryUsageItem {
  cat_id:        string | null;
  name:          string;
  color:         string;
  duration_secs: number;
  user_count:    number;
  pct:           number;
}

export interface CategoryUsageListOut {
  items:       CategoryUsageItem[];
  total_secs:  number;
  total_users: number;
}

export interface CategoryDetailActivity { app_name: string; duration_secs: number; pct: number; }
export interface CategoryDetailUser     { name: string; email: string; duration_secs: number; pct: number; }

// ── URL Policies ──────────────────────────────────────────────────────────────

export interface BlockedUrl {
  id:          string;
  pattern:     string;
  target_type: "all" | "device" | "employee";
  device_id:   string | null;
  employee_id: string | null;
  notes:       string | null;
  is_active:   boolean;
  created_at:  string;
}

export interface CategoryUsageDetailOut {
  cat_id:     string | null;
  name:       string;
  color:      string;
  total_secs: number;
  activities: CategoryDetailActivity[];
  users:      CategoryDetailUser[];
}
