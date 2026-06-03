import type {
  Device, TimelineEvent, DailyStat, Screenshot, CompanyDay, AppUsage, LogEvent,
  Me, Role, ModuleDef, User, Group, GroupDetail, GroupMember, Installer,
  OverviewStats, RealtimeDevice, WebUsage,
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
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
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
export const createUser = (body: Record<string, unknown>) =>
  request<User>("/v1/users", { method: "POST", body: JSON.stringify(body) });
export const updateUser = (id: string, body: Record<string, unknown>) =>
  request<User>(`/v1/users/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteUser = (id: string) =>
  request<void>(`/v1/users/${id}`, { method: "DELETE" });

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

// ── Home dashboards ─────────────────────────────────────────────────────────

const gq = (group_id?: string, extra?: Record<string, string | number>) => {
  const p = new URLSearchParams();
  if (group_id) p.set("group_id", group_id);
  for (const [k, v] of Object.entries(extra ?? {})) p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const getOverview = (group_id?: string, days = 7) =>
  request<OverviewStats>(`/v1/home/overview${gq(group_id, { days })}`);
export const getRealtime = (group_id?: string) =>
  request<RealtimeDevice[]>(`/v1/home/realtime${gq(group_id)}`);
export const getHomeAppUsage = (group_id?: string, days = 1) =>
  request<AppUsage[]>(`/v1/home/app-usage${gq(group_id, { days })}`);
export const getHomeWebUsage = (group_id?: string, days = 1) =>
  request<WebUsage[]>(`/v1/home/web-usage${gq(group_id, { days })}`);

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
