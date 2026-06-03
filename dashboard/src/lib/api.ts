import type { Device, TimelineEvent, DailyStat, Screenshot, CompanyDay, AppUsage, LogEvent } from "@/types";

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
