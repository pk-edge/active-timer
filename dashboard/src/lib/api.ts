import type {
  Device, TimelineEvent, DailyStat, CompanyDay, AppUsage, LogEvent,
  Me, Role, ModuleDef, User, UserActivity, UserActivityDetail,
  Group, GroupDetail, GroupMember, Installer, InstallerDownload, UpdateRunsResponse,
  OverviewStats, HomeWidgets, RealtimeDevice, WebUsage, MyStats, Notification, MeetingsResponse,
  AppCategory, CategoriesPage, TechSummary, CategoryAllocationItem, AppTrends, AppDetailItem, AdoptionData,
  UncategorizedData,
  WebsiteListOut, WebsiteDetailOut,
  AppUsageListOut, AppUsageDetailOut,
  CategoryUsageListOut, CategoryUsageDetailOut,
} from "@/types";

export const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

function getToken(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("tt_token") ?? "";
}

function authHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${getToken()}`,
  };
}

/** Thrown when a read is aborted — kept for any caller that checks for it. */
export class AbortedError extends Error {
  readonly aborted = true;
  constructor() {
    super("Request aborted");
    this.name = "AbortedError";
  }
}

// ── Navigation-scoped request cancellation ────────────────────────────────────
// Every GET issued through request() is tied to the current "page generation".
// When the route changes (see components/NavigationAbort.tsx) the previous
// generation's controller is aborted: the browser drops the sockets and the
// backend cancels the matching queries, so a heavy page you navigated away
// from stops costing anything. Mutations (POST/PATCH/…) are never auto-aborted.
let navController: AbortController | null = null;

function navSignal(): AbortSignal | undefined {
  if (typeof window === "undefined") return undefined;
  if (!navController) navController = new AbortController();
  return navController.signal;
}

/** Abort all in-flight GETs from the previous page. Called on route change. */
export function abortInFlightGets(): void {
  navController?.abort();
  navController = new AbortController();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const signal = init?.signal ?? (method === "GET" ? navSignal() : undefined);
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      cache: "no-store",
      ...init,
      signal,
      headers: { ...authHeaders(), ...init?.headers },
    });
  } catch (err) {
    // A navigation-aborted fetch rejects with a DOMException named "AbortError".
    // Re-throw it as our AbortedError so SWR recognizes it and does NOT retry —
    // otherwise an aborted GET was treated as a real error and re-sent, which
    // showed up as "request cancelled, then immediately fired again".
    if (signal?.aborted || (err as { name?: string })?.name === "AbortError") {
      throw new AbortedError();
    }
    throw err;
  }

  if (res.status === 401) {
    if (typeof window !== "undefined") {
      localStorage.removeItem("tt_token");
      window.location.href = "/login";
    }
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    const body = await res.text();
    let msg = `${res.status}: ${body}`;
    try {
      const j = JSON.parse(body);
      if (j.detail) msg = j.detail;
    } catch { /* non-JSON body */ }
    throw new Error(msg);
  }

  if (res.status === 204 || res.headers.get("content-length") === "0") {
    return undefined as unknown as T;
  }
  return res.json() as Promise<T>;
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export async function adminLogin(email: string, password: string) {
  const data = await request<{ access_token: string; role: string; name: string }>(
    "/v1/auth/admin/login",
    { method: "POST", body: JSON.stringify({ email, password }) },
  );
  localStorage.setItem("tt_token", data.access_token);
  return data;
}

export function adminLogout() {
  localStorage.removeItem("tt_token");
  window.location.href = "/login";
}

export function isAuthenticated(): boolean {
  return Boolean(getToken());
}

export function loginWithToken(token: string) {
  localStorage.setItem("tt_token", token);
}

export async function setPassword(password: string): Promise<void> {
  await request<void>("/v1/auth/set-password", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

// ── Devices ───────────────────────────────────────────────────────────────────

export const getDevices = (showArchived = false) =>
  request<Device[]>(`/v1/devices${showArchived ? "?show_archived=true" : ""}`);

export const assignDevice = (deviceId: string, employeeId: string | null) =>
  request<Device>(`/v1/devices/${deviceId}/assign`, {
    method: "PATCH",
    body: JSON.stringify({ employee_id: employeeId }),
  });

export const archiveDevice = (deviceId: string) =>
  request<Device>(`/v1/devices/${deviceId}/archive`, { method: "PATCH" });

export const restoreDevice = (deviceId: string) =>
  request<Device>(`/v1/devices/${deviceId}/restore`, { method: "PATCH" });

export const deleteDevice = (deviceId: string) =>
  request<void>(`/v1/devices/${deviceId}`, { method: "DELETE" });

export const getTimeline = (deviceId: string, since?: number, until?: number) => {
  const params = new URLSearchParams();
  if (since !== undefined) params.set("since", String(since));
  if (until !== undefined) params.set("until", String(until));
  const qs = params.toString();
  return request<TimelineEvent[]>(`/v1/devices/${deviceId}/timeline${qs ? `?${qs}` : ""}`);
};

export const getDailyStats = (deviceId: string, days = 7) =>
  request<DailyStat[]>(`/v1/devices/${deviceId}/stats?days=${days}`);


// ── Reports ───────────────────────────────────────────────────────────────────

export const getCompanySummary = (days = 7) =>
  request<CompanyDay[]>(`/v1/reports/company/summary?days=${days}`);

export const getTopApps = (days = 7) =>
  request<AppUsage[]>(`/v1/reports/company/top-apps?days=${days}`);

export const getDeviceSummary = (deviceId: string, days = 7) =>
  request<CompanyDay[]>(`/v1/reports/device/${deviceId}/summary?days=${days}`);

export const getDeviceTopApps = (deviceId: string, days = 7) =>
  request<AppUsage[]>(`/v1/reports/device/${deviceId}/top-apps?days=${days}`);

// ── Logs ──────────────────────────────────────────────────────────────────────

export const getLogs = (opts?: { since?: number; until?: number; device_id?: string; employee_id?: string; event_type?: string }) => {
  const params = new URLSearchParams();
  if (opts?.since !== undefined) params.set("since",        String(opts.since));
  if (opts?.until !== undefined) params.set("until",        String(opts.until));
  if (opts?.device_id)           params.set("device_id",    opts.device_id);
  if (opts?.employee_id)         params.set("employee_id",  opts.employee_id);
  if (opts?.event_type)          params.set("event_type",   opts.event_type);
  const qs = params.toString();
  return request<LogEvent[]>(`/v1/logs${qs ? `?${qs}` : ""}`);
};

// ── Me / RBAC ───────────────────────────────────────────────────────────────

export const getMe = () => request<Me>("/v1/auth/me");

export const getModules = () => request<ModuleDef[]>("/v1/roles/modules");
export const getRoles   = () => request<Role[]>("/v1/roles");
export const createRole = (body: Partial<Role>) =>
  request<Role>("/v1/roles", { method: "POST", body: JSON.stringify(body) });
export const updateRole = (id: string, body: Partial<Role>) =>
  request<Role>(`/v1/roles/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteRole = (id: string) =>
  request<void>(`/v1/roles/${id}`, { method: "DELETE" });

// ── Users ─────────────────────────────────────────────────────────────────────

export const getUsers = (search?: string) =>
  request<User[]>(`/v1/users${search ? `?search=${encodeURIComponent(search)}` : ""}`);
export const getUserActivity = () =>
  request<UserActivity[]>("/v1/users/activity");
export const getUserActivityDetail = (id: string) =>
  request<UserActivityDetail>(`/v1/users/${id}/activity`);
export const getPendingUsers = () =>
  request<User[]>("/v1/users?pending=true");
export const createUser = (body: Record<string, unknown>) =>
  request<User>("/v1/users", { method: "POST", body: JSON.stringify(body) });
export const updateUser = (id: string, body: Record<string, unknown>) =>
  request<User>(`/v1/users/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteUser = (id: string) =>
  request<void>(`/v1/users/${id}`, { method: "DELETE" });
export const deactivateUser = (id: string) =>
  request<User>(`/v1/users/${id}/deactivate`, { method: "POST" });
export const activateUser = (id: string) =>
  request<User>(`/v1/users/${id}/activate`, { method: "POST" });
export const approveUser = (id: string, body: { role_id: string; group_ids: string[] }) =>
  request<User>(`/v1/users/${id}/approve`, { method: "POST", body: JSON.stringify(body) });

export async function registerUser(body: { name: string; email: string; password: string }) {
  const res = await fetch(`${BASE}/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({ detail: "Registration failed" }));
    throw new Error(data.detail ?? "Registration failed");
  }
  return res.json() as Promise<{ message: string }>;
}

// ── Groups ──────────────────────────────────────────────────────────────────

export const getGroups  = (includeArchived = false) =>
  request<Group[]>(`/v1/groups${includeArchived ? "?include_archived=true" : ""}`);
export const getGroup   = (id: string) => request<GroupDetail>(`/v1/groups/${id}`);
export const createGroup = (body: { name: string; description?: string; parent_id?: string | null }) =>
  request<Group>("/v1/groups", { method: "POST", body: JSON.stringify(body) });
export const updateGroup = (id: string, body: { name: string; description?: string; parent_id?: string | null }) =>
  request<Group>(`/v1/groups/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteGroup = (id: string) =>
  request<void>(`/v1/groups/${id}`, { method: "DELETE" });
export const addGroupMember = (id: string, body: { member_type: string; employee_id?: string; device_id?: string }) =>
  request<GroupMember>(`/v1/groups/${id}/members`, { method: "POST", body: JSON.stringify(body) });
export const removeGroupMember = (id: string, memberId: string) =>
  request<void>(`/v1/groups/${id}/members/${memberId}`, { method: "DELETE" });
export const getGroupMembers = (id: string) => request<GroupMember[]>(`/v1/groups/${id}/members`);
export const archiveGroup = (id: string) =>
  request<void>(`/v1/groups/${id}/archive`, { method: "POST" });
export const unarchiveGroup = (id: string) =>
  request<void>(`/v1/groups/${id}/unarchive`, { method: "POST" });

export async function importGroupsCsv(file: File) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${BASE}/v1/groups/import`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: fd,
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

// ── Installers / agents ───────────────────────────────────────────────────────

export const getInstallers = () => request<Installer[]>("/v1/installers");
export const activateInstaller = (id: string) =>
  request<Installer>(`/v1/installers/${id}/activate`, { method: "POST" });
export const deleteInstaller = (id: string) =>
  request<void>(`/v1/installers/${id}`, { method: "DELETE" });
export const getInstallerDownload = (id: string) =>
  request<{ download_url: string; version: string; platform: string }>(`/v1/installers/${id}/download`);
export const getInstallerDownloads = () => request<InstallerDownload[]>("/v1/installers/downloads");

export const patchInstaller = (id: string, body: {
  rollout_percent?: number; mandatory?: boolean; channel?: string;
  min_supported_version?: string | null; expires_at?: string | null;
}) => request<Installer>(`/v1/installers/${id}`, {
  method: "PATCH",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const getUpdateRuns = () => request<UpdateRunsResponse>("/v1/updates/runs");

export async function uploadInstaller(form: {
  platform: string; installer_type: string; version: string; notes?: string; file: File;
}) {
  const fd = new FormData();
  fd.append("platform", form.platform);
  fd.append("installer_type", form.installer_type);
  fd.append("version", form.version);
  if (form.notes) fd.append("notes", form.notes);
  fd.append("file", form.file);
  const res = await fetch(`${BASE}/v1/installers`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: fd,
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json() as Promise<Installer>;
}

// ── Audit ─────────────────────────────────────────────────────────────────────

export interface AuditEntry {
  id: string; action: string; target_type: string | null; target_id: string | null;
  metadata: Record<string, unknown> | null;
  actor_id: string | null; actor_name: string | null; actor_email: string | null; actor_role: string | null;
  created_at: string | null; created_at_unix: number | null;
}

export interface AuditPage { total: number; offset: number; limit: number; entries: AuditEntry[]; }

export interface AuditFilters {
  limit?: number; offset?: number;
  action?: string; target_type?: string; actor_id?: string;
  since?: string; until?: string; search?: string;
}

export interface AuditFilterOptions {
  actions: string[];
  target_types: string[];
  actors: { id: string; name: string; email: string }[];
}

export function getAudit(filters: AuditFilters = {}) {
  const p = new URLSearchParams();
  if (filters.limit)       p.set("limit",       String(filters.limit));
  if (filters.offset)      p.set("offset",      String(filters.offset));
  if (filters.action)      p.set("action",      filters.action);
  if (filters.target_type) p.set("target_type", filters.target_type);
  if (filters.actor_id)    p.set("actor_id",    filters.actor_id);
  if (filters.since)       p.set("since",       filters.since);
  if (filters.until)       p.set("until",       filters.until);
  if (filters.search)      p.set("search",      filters.search);
  return request<AuditPage>(`/v1/audit?${p.toString()}`);
}

export const getAuditFilterOptions = () => request<AuditFilterOptions>("/v1/audit/filters");

// ── Home dashboards ─────────────────────────────────────────────────────────

const gq = (group_id?: string, extra?: Record<string, string | number>) => {
  const p = new URLSearchParams();
  if (group_id) p.set("group_id", group_id);
  for (const [k, v] of Object.entries(extra ?? {})) p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const getOverview = (
  group_id?: string,
  days = 7,
  since_date?: string,
  until_date?: string,
) => {
  const extra: Record<string, string | number> = { days };
  if (since_date) extra.since_date = since_date;
  if (until_date) extra.until_date = until_date;
  return request<OverviewStats>(`/v1/home/overview${gq(group_id, extra)}`);
};

export const getHomeWidgets = (
  group_id?: string,
  app_days = 1,
  app_limit = 12,
) => {
  const extra: Record<string, string | number> = { app_days, app_limit };
  return request<HomeWidgets>(`/v1/home/widgets${gq(group_id, extra)}`);
};
export const getRealtime = (group_id?: string) =>
  request<RealtimeDevice[]>(`/v1/home/realtime${gq(group_id)}`);
export const getHomeAppUsage = (group_id?: string, days = 1) =>
  request<AppUsage[]>(`/v1/home/app-usage${gq(group_id, { days })}`);
export const getHomeWebUsage = (group_id?: string, days = 1) =>
  request<WebUsage[]>(`/v1/home/web-usage${gq(group_id, { days })}`);
export const getMyStats = () => request<MyStats>("/v1/home/my-stats");
export const getMeetings = (opts: { deviceId?: string; employeeId?: string; days?: number }) => {
  const p = new URLSearchParams();
  if (opts.deviceId)   p.set("device_id", opts.deviceId);
  if (opts.employeeId) p.set("employee_id", opts.employeeId);
  p.set("days", String(opts.days ?? 7));
  return request<MeetingsResponse>(`/v1/home/meetings?${p.toString()}`);
};

// ── Notifications ─────────────────────────────────────────────────────────────

export const getNotifications = () => request<Notification[]>("/v1/notifications");
export const markNotificationsRead = (keys: string[]) =>
  request<void>("/v1/notifications/mark-read", {
    method: "POST",
    body: JSON.stringify({ keys }),
  });

// ── Technology & AI Usage ─────────────────────────────────────────────────────

export const getTechCategories = (params?: { page?: number; per_page?: number; is_system?: boolean }) => {
  const q = new URLSearchParams();
  if (params?.page)     q.set("page",      String(params.page));
  if (params?.per_page) q.set("per_page",  String(params.per_page));
  if (params?.is_system !== undefined) q.set("is_system", String(params.is_system));
  const qs = q.toString();
  return request<CategoriesPage>(`/v1/tech/categories${qs ? `?${qs}` : ""}`);
};

export const createTechCategory = (body: { name: string; color: string; description?: string }) =>
  request<AppCategory>("/v1/tech/categories", { method: "POST", body: JSON.stringify(body) });

export const updateTechCategory = (id: string, body: { name?: string; color?: string; description?: string }) =>
  request<AppCategory>(`/v1/tech/categories/${id}`, { method: "PUT", body: JSON.stringify(body) });

export const deleteTechCategory = (id: string) =>
  request<void>(`/v1/tech/categories/${id}`, { method: "DELETE" });

export const addCategoryRule = (catId: string, app_pattern: string) =>
  request<AppCategory>(`/v1/tech/categories/${catId}/rules`, {
    method: "POST",
    body: JSON.stringify({ app_pattern }),
  });

export const deleteCategoryRule = (ruleId: string) =>
  request<void>(`/v1/tech/categories/rules/${ruleId}`, { method: "DELETE" });

// ── date helpers used by tech pages ──────────────────────────────────────────

export function techDateParams(since: string, until: string): URLSearchParams {
  return new URLSearchParams({ since_date: since, until_date: until });
}

// ── Tech Utilization ──────────────────────────────────────────────────────────

export const getTechSummary = (since: string, until: string, group_id?: string, employee_id?: string) => {
  const p = techDateParams(since, until);
  if (group_id)    p.set("group_id",    group_id);
  if (employee_id) p.set("employee_id", employee_id);
  return request<TechSummary>(`/v1/tech/utilization/summary?${p}`);
};

export const getCategoryAllocation = (since: string, until: string, group_id?: string, employee_id?: string) => {
  const p = techDateParams(since, until);
  if (group_id)    p.set("group_id",    group_id);
  if (employee_id) p.set("employee_id", employee_id);
  return request<CategoryAllocationItem[]>(`/v1/tech/utilization/category-allocation?${p}`);
};

export const getAppTrends = (since: string, until: string, limit = 10) => {
  const p = techDateParams(since, until);
  p.set("limit", String(limit));
  return request<AppTrends>(`/v1/tech/utilization/app-trends?${p}`);
};

export const getAppList = (since: string, until: string, category_id?: string, limit = 50, group_id?: string, employee_id?: string) => {
  const p = techDateParams(since, until);
  p.set("limit", String(limit));
  if (category_id) p.set("category_id", category_id);
  if (group_id)    p.set("group_id",    group_id);
  if (employee_id) p.set("employee_id", employee_id);
  return request<AppDetailItem[]>(`/v1/tech/utilization/apps?${p}`);
};

export const getAdoptionData = (since: string, until: string, top_n = 5) => {
  const p = techDateParams(since, until);
  p.set("top_n", String(top_n));
  return request<AdoptionData>(`/v1/tech/utilization/adoption?${p}`);
};

export const getUncategorized = (since: string, until: string) => {
  const p = techDateParams(since, until);
  return request<UncategorizedData>(`/v1/tech/uncategorized?${p}`);
};

// ── Website Usage ─────────────────────────────────────────────────────────────

export const getWebsites = (since: string, until: string, productivity?: string, group_id?: string, employee_id?: string) => {
  const p = techDateParams(since, until);
  if (productivity) p.set("productivity", productivity);
  if (group_id)     p.set("group_id",     group_id);
  if (employee_id)  p.set("employee_id",  employee_id);
  return request<WebsiteListOut>(`/v1/tech/websites?${p}`);
};

export const getWebsiteDetail = (host: string, tab: string, since: string, until: string, group_id?: string, employee_id?: string) => {
  const p = techDateParams(since, until);
  p.set("host", host); p.set("tab", tab);
  if (group_id)    p.set("group_id",    group_id);
  if (employee_id) p.set("employee_id", employee_id);
  return request<WebsiteDetailOut>(`/v1/tech/websites/detail?${p}`);
};

// ── Application Usage ─────────────────────────────────────────────────────────

export const getAppUsage = (since: string, until: string, productivity?: string, group_id?: string, employee_id?: string) => {
  const p = techDateParams(since, until);
  if (productivity) p.set("productivity", productivity);
  if (group_id)     p.set("group_id",     group_id);
  if (employee_id)  p.set("employee_id",  employee_id);
  return request<AppUsageListOut>(`/v1/tech/apps?${p}`);
};

export const getAppUsageDetail = (app_name: string, tab: string, since: string, until: string, group_id?: string, employee_id?: string) => {
  const p = techDateParams(since, until);
  p.set("app_name", app_name); p.set("tab", tab);
  if (group_id)    p.set("group_id",    group_id);
  if (employee_id) p.set("employee_id", employee_id);
  return request<AppUsageDetailOut>(`/v1/tech/apps/detail?${p}`);
};

// ── Category Usage ────────────────────────────────────────────────────────────

export const getCategoryUsage = (since: string, until: string, group_id?: string) => {
  const p = techDateParams(since, until);
  if (group_id) p.set("group_id", group_id);
  return request<CategoryUsageListOut>(`/v1/tech/category-usage?${p}`);
};

export const getCategoryUsageDetail = (cat_id: string | null, tab: string, since: string, until: string, group_id?: string) => {
  const p = techDateParams(since, until);
  p.set("tab", tab);
  if (cat_id)   p.set("cat_id", cat_id);
  if (group_id) p.set("group_id", group_id);
  return request<CategoryUsageDetailOut>(`/v1/tech/category-usage/detail?${p}`);
};

// ── Company ───────────────────────────────────────────────────────────────────

export interface CompanySettings {
  timezone: string;
  work_start: string;
  work_end: string;
  track_urls: boolean;
  track_window_titles: boolean;
  track_keyboard: boolean;
  idle_timeout_mins: number;
  email_alerts: boolean;
  idle_alert_mins: number;
  offline_alert_mins: number;
  daily_report: boolean;
  max_file_size_mb: number;
}

export interface CompanyInfo {
  name: string;
  enrollment_token: string;
  agent_headless: boolean;
  inactive_device_days: number | null;
  settings: CompanySettings;
}

export const getCompanyInfo = () => request<CompanyInfo>("/v1/company/info");
export const updateCompanySettings = (body: {
  agent_headless: boolean;
  inactive_device_days: number | null;
  name?: string;
  settings?: Partial<CompanySettings>;
}) =>
  request<CompanyInfo>("/v1/company/settings", {
    method: "PATCH",
    body: JSON.stringify(body),
  });
// Per-device visibility override: null = inherit company default, true/false = force.
export const setDeviceHeadless = (deviceId: string, override: boolean | null) =>
  request<Device>(`/v1/devices/${deviceId}/headless`, {
    method: "PATCH",
    body: JSON.stringify({ override }),
  });

// ── URL Policies ─────────────────────────────────────────────────────────────

import type { BlockedUrl } from "@/types";

export const getDetectedUrls = () =>
  request<{ host: string; visits: number }[]>("/v1/policy/detected-urls");

export const getBlockedUrls = (is_active?: boolean) => {
  const p = new URLSearchParams();
  if (is_active !== undefined) p.set("is_active", String(is_active));
  return request<BlockedUrl[]>(`/v1/policy/blocked-urls/admin${p.toString() ? `?${p}` : ""}`);
};

export const createBlockedUrl = (body: {
  pattern: string; target_type: string;
  device_id?: string; employee_id?: string; notes?: string;
}) => request<BlockedUrl>("/v1/policy/blocked-urls", { method: "POST", body: JSON.stringify(body) });

export const updateBlockedUrl = (id: string, body: Partial<{
  pattern: string; target_type: string; device_id: string | null;
  employee_id: string | null; notes: string | null; is_active: boolean;
}>) => request<BlockedUrl>(`/v1/policy/blocked-urls/${id}`, { method: "PATCH", body: JSON.stringify(body) });

export const deleteBlockedUrl = (id: string) =>
  request<void>(`/v1/policy/blocked-urls/${id}`, { method: "DELETE" });

// ── Fleet & device detailed reports ──────────────────────────────────────────

export interface FleetReport {
  since: string;
  until: string;
  total_devices: number;
  online_devices: number;
  offline_devices: number;
  archived_devices: number;
  platform_breakdown: { platform: string; count: number }[];
  daily_activity: { date: string; active_seconds: number; idle_seconds: number; device_count: number }[];
  top_apps: { app_name: string; seconds: number }[];
  most_active_devices: { device_id: string; hostname: string; employee_name: string | null; active_seconds: number }[];
}

export interface DeviceFullReport {
  device: {
    id: string; hostname: string; platform: string;
    employee_name: string | null; enrolled_at: string; last_seen: string | null;
  };
  since: string;
  until: string;
  total_active_seconds: number;
  total_idle_seconds: number;
  working_days: number;
  daily_breakdown: { date: string; active_seconds: number; idle_seconds: number }[];
  top_apps: { app_name: string; seconds: number; pct: number }[];
  top_urls: { url: string; visits: number }[];
}

export const getFleetReport = (since: string, until: string) =>
  request<FleetReport>(`/v1/reports/devices/fleet?since=${since}&until=${until}`);

export const getDeviceFullReport = (deviceId: string, since: string, until: string) =>
  request<DeviceFullReport>(`/v1/reports/device/${deviceId}/full?since=${since}&until=${until}`);

// ── Helpers ───────────────────────────────────────────────────────────────────

export function fmtSeconds(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function fmtBytes(bytes: number): string {
  return bytes < 1024 ? `${bytes}B` : `${(bytes / 1024).toFixed(0)}KB`;
}

// ── Shared Files ──────────────────────────────────────────────────────────────

import type { SharedFile, SharedFilesPage } from "@/types";

export function getSharedFiles(q?: string, offset = 0, limit = 20): Promise<SharedFilesPage> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (q) qs.set("q", q);
  return request<SharedFilesPage>(`/v1/files?${qs}`);
}

export const getFileLimits = (): Promise<{ max_file_size_mb: number }> =>
  request<{ max_file_size_mb: number }>("/v1/files/limits");

export async function uploadSharedFile(file: File): Promise<SharedFile> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${BASE}/v1/files`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: fd,
  });
  if (!res.ok) {
    const body = await res.text();
    let msg = `${res.status}: ${body}`;
    try { const j = JSON.parse(body); if (j.detail) msg = j.detail; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return res.json() as Promise<SharedFile>;
}

export const deleteSharedFile = (id: string): Promise<void> =>
  request<void>(`/v1/files/${id}`, { method: "DELETE" });
