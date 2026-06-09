import type {
  Device, TimelineEvent, DailyStat, Screenshot, CompanyDay, AppUsage, LogEvent,
  Me, Role, ModuleDef, User, Group, GroupDetail, GroupMember, Installer,
  OverviewStats, RealtimeDevice, WebUsage, MyStats, Notification,
  AppCategory, TechSummary, CategoryAllocationItem, AppTrends, AppDetailItem, AdoptionData,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    // Always hit the network — analytics data must never be served stale from
    // the browser/HTTP cache when the user (or SWR) explicitly refetches.
    cache: "no-store",
    ...init,
    headers: { ...authHeaders(), ...init?.headers },
  });

  if (res.status === 401) {
    if (typeof window !== "undefined") {
      localStorage.removeItem("tt_token");
      window.location.href = "/login";
    }
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    // Surface detail message from FastAPI JSON errors so callers can inspect them.
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

// ── Devices ───────────────────────────────────────────────────────────────────

export const getDevices = () => request<Device[]>("/v1/devices");

export const getTimeline = (deviceId: string, since?: number, until?: number) => {
  const params = new URLSearchParams();
  if (since !== undefined) params.set("since", String(since));
  if (until !== undefined) params.set("until", String(until));
  const qs = params.toString();
  return request<TimelineEvent[]>(`/v1/devices/${deviceId}/timeline${qs ? `?${qs}` : ""}`);
};

export const getDailyStats = (deviceId: string, days = 7) =>
  request<DailyStat[]>(`/v1/devices/${deviceId}/stats?days=${days}`);

// ── Screenshots ───────────────────────────────────────────────────────────────

export const getScreenshots = (deviceId: string) =>
  request<Screenshot[]>(`/v1/screenshots/${deviceId}`);

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

export const getLogs = (opts?: { since?: number; until?: number; device_id?: string; event_type?: string }) => {
  const params = new URLSearchParams();
  if (opts?.since !== undefined) params.set("since",      String(opts.since));
  if (opts?.until !== undefined) params.set("until",      String(opts.until));
  if (opts?.device_id)           params.set("device_id",  opts.device_id);
  if (opts?.event_type)          params.set("event_type", opts.event_type);
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

export const getGroups  = () => request<Group[]>("/v1/groups");
export const getGroup   = (id: string) => request<GroupDetail>(`/v1/groups/${id}`);
export const createGroup = (body: { name: string; description?: string }) =>
  request<Group>("/v1/groups", { method: "POST", body: JSON.stringify(body) });
export const updateGroup = (id: string, body: { name: string; description?: string }) =>
  request<Group>(`/v1/groups/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteGroup = (id: string) =>
  request<void>(`/v1/groups/${id}`, { method: "DELETE" });
export const addGroupMember = (id: string, body: { member_type: string; employee_id?: string; device_id?: string }) =>
  request<GroupMember>(`/v1/groups/${id}/members`, { method: "POST", body: JSON.stringify(body) });
export const removeGroupMember = (id: string, memberId: string) =>
  request<void>(`/v1/groups/${id}/members/${memberId}`, { method: "DELETE" });

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
export const getInstallerDownload = (id: string) =>
  request<{ download_url: string; version: string; platform: string }>(`/v1/installers/${id}/download`);

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
  metadata: Record<string, unknown> | null; actor_name: string | null; actor_email: string | null;
  created_at: string | null; created_at_unix: number | null;
}
export const getAudit = (limit = 100) => request<AuditEntry[]>(`/v1/audit?limit=${limit}`);

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
export const getRealtime = (group_id?: string) =>
  request<RealtimeDevice[]>(`/v1/home/realtime${gq(group_id)}`);
export const getHomeAppUsage = (group_id?: string, days = 1) =>
  request<AppUsage[]>(`/v1/home/app-usage${gq(group_id, { days })}`);
export const getHomeWebUsage = (group_id?: string, days = 1) =>
  request<WebUsage[]>(`/v1/home/web-usage${gq(group_id, { days })}`);
export const getMyStats = () => request<MyStats>("/v1/home/my-stats");

// ── Notifications ─────────────────────────────────────────────────────────────

export const getNotifications = () => request<Notification[]>("/v1/notifications");
export const markNotificationsRead = (keys: string[]) =>
  request<void>("/v1/notifications/mark-read", {
    method: "POST",
    body: JSON.stringify({ keys }),
  });

// ── Technology & AI Usage ─────────────────────────────────────────────────────

export const getTechCategories = () =>
  request<AppCategory[]>("/v1/tech/categories");

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
