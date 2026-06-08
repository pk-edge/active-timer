"use client";
import Link from "next/link";
import useSWR from "swr";
import {
  Monitor, BarChart2, Users2, DownloadCloud,
  Wifi, Coffee, WifiOff, Clock, Activity as ActivityIcon,
  Laptop, AppWindow, Globe,
} from "lucide-react";
import {
  getOverview, getRealtime, getHomeAppUsage, getMyStats,
  fmtSeconds, fmtTime,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied, StatusDonut, UsagePie } from "@/components/charts";
import RefreshButton from "@/components/RefreshButton";
import type { Me, OverviewStats, RealtimeDevice, AppUsage, MyStats } from "@/types";

const STATUS_STYLE: Record<string, string> = {
  active:   "bg-green-50 text-green-700 border-green-200",
  passive:  "bg-amber-50 text-amber-700 border-amber-200",
  inactive: "bg-gray-100 text-gray-500 border-gray-200",
};

const EVENT_LABEL: Record<string, string> = {
  window_change: "Switched app",
  url_visit:     "Visited URL",
  idle_start:    "Went idle",
  lock:          "Screen locked",
  idle_end:      "Returned",
};

function StatCard({ label, value, sub, color = "text-gray-900" }: {
  label: string; value: string | number; sub?: string; color?: string;
}) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <p className={`text-2xl font-bold ${color} leading-none`}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
      <p className="text-xs text-gray-500 mt-1">{label}</p>
    </div>
  );
}

// ── Admin / Manager home ──────────────────────────────────────────────────────

function AdminHome({ me }: { me: Me }) {
  const { can } = useUser();
  const today = new Date().toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric",
  });

  const { data: stats, mutate } = useSWR<OverviewStats>(
    "home-overview",
    () => getOverview(undefined, 7),
    { refreshInterval: 15_000 },
  );
  const { data: realtime } = useSWR<RealtimeDevice[]>(
    "home-realtime",
    () => getRealtime(),
    { refreshInterval: 10_000 },
  );
  const { data: apps } = useSWR<AppUsage[]>(
    "home-apps",
    () => getHomeAppUsage(undefined, 1),
  );

  const s = stats?.status ?? { active: 0, passive: 0, inactive: 0 };
  const avgHrs = ((stats?.avg_active_seconds_today ?? 0) / 3600).toFixed(1);

  const QUICK_ACTIONS = [
    { href: "/devices",  label: "Devices",  icon: Monitor,      module: "devices" },
    { href: "/reports",  label: "Reports",  icon: BarChart2,    module: "reports" },
    { href: "/users",    label: "Users",    icon: Users2,        module: "users" },
    { href: "/agents",   label: "Agents",   icon: DownloadCloud, module: "agents" },
  ];

  return (
    <div className="overflow-y-auto pr-1">
      {/* Welcome header */}
      <div className="flex flex-wrap items-start justify-between gap-3 mb-6">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">
            Welcome back, {me.name}!
          </h1>
          <p className="text-sm text-gray-500 mt-0.5">
            <span className="font-medium text-blue-600">{me.role_name}</span> · {today}
          </p>
        </div>
        <RefreshButton onRefresh={() => mutate()} />
      </div>

      {/* Quick actions */}
      <div className="flex flex-wrap gap-3 mb-6">
        {QUICK_ACTIONS.filter(a => can(a.module, "view")).map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 hover:border-blue-400 hover:text-blue-600 shadow-sm transition"
          >
            <Icon size={15} />
            {label}
          </Link>
        ))}
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <StatCard label="Total devices"    value={stats?.total_devices ?? 0} />
        <StatCard label="Active today"     value={stats?.active_today ?? 0}  color="text-green-600" />
        <StatCard label="Online right now" value={s.active}                  color="text-blue-600" />
        <StatCard label="Avg active / device" value={`${avgHrs}h`}           color="text-purple-600" />
      </div>

      {/* Donut + Top Apps */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <ActivityIcon size={15} className="text-gray-400" />
            <p className="text-sm font-semibold text-gray-800">Live Activity Status</p>
          </div>
          <div className="grid grid-cols-2 gap-4 items-center">
            <StatusDonut active={s.active} passive={s.passive} inactive={s.inactive} />
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <Wifi size={18} className="text-green-500" />
                <div><p className="text-xl font-bold text-gray-900 leading-none">{s.active}</p><p className="text-xs text-gray-500 mt-0.5">Active now</p></div>
              </div>
              <div className="flex items-center gap-3">
                <Coffee size={18} className="text-amber-500" />
                <div><p className="text-xl font-bold text-gray-900 leading-none">{s.passive}</p><p className="text-xs text-gray-500 mt-0.5">Passive now</p></div>
              </div>
              <div className="flex items-center gap-3">
                <WifiOff size={18} className="text-gray-400" />
                <div><p className="text-xl font-bold text-gray-900 leading-none">{s.inactive}</p><p className="text-xs text-gray-500 mt-0.5">Inactive now</p></div>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <AppWindow size={15} className="text-gray-400" />
            <p className="text-sm font-semibold text-gray-800">Top Applications (today)</p>
          </div>
          <UsagePie items={apps ?? []} labelKey="app_name" valueKey="seconds" format={fmtSeconds} />
        </div>
      </div>

      {/* Realtime device table */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <div className="flex items-center gap-2 px-5 py-4 border-b border-gray-100">
          <Clock size={15} className="text-gray-400" />
          <p className="text-sm font-semibold text-gray-800">Live Device Feed</p>
          <span className="ml-auto text-xs text-gray-400">{realtime?.length ?? 0} devices</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[640px]">
          <thead>
            <tr className="bg-gray-50 text-gray-400 text-xs uppercase tracking-wide">
              <th className="text-left px-5 py-2.5 font-semibold">Employee</th>
              <th className="text-left px-5 py-2.5 font-semibold">Device</th>
              <th className="text-left px-5 py-2.5 font-semibold">Status</th>
              <th className="text-left px-5 py-2.5 font-semibold">Current app</th>
              <th className="text-left px-5 py-2.5 font-semibold">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {(realtime ?? []).slice(0, 15).map(d => (
              <tr key={d.device_id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-5 py-3 font-medium text-gray-800">
                  <Link href={`/devices/${d.device_id}`} className="hover:text-blue-600 transition">
                    {d.employee_name ?? "—"}
                  </Link>
                </td>
                <td className="px-5 py-3 text-gray-500">{d.hostname}</td>
                <td className="px-5 py-3">
                  <span className={`text-xs px-2 py-0.5 rounded-full border capitalize ${STATUS_STYLE[d.status]}`}>
                    {d.status}
                  </span>
                </td>
                <td className="px-5 py-3 text-gray-600 max-w-[180px] truncate">
                  {d.current_app ?? <span className="text-gray-300">—</span>}
                </td>
                <td className="px-5 py-3 text-gray-400 text-xs">
                  {d.last_seen_unix ? fmtTime(d.last_seen_unix) : "—"}
                </td>
              </tr>
            ))}
            {(realtime ?? []).length === 0 && (
              <tr><td colSpan={5} className="px-5 py-10 text-center text-gray-400">No devices yet.</td></tr>
            )}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  );
}

// ── Employee home ─────────────────────────────────────────────────────────────

function EmployeeHome({ me }: { me: Me }) {
  const today = new Date().toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric",
  });

  const { data: stats } = useSWR<MyStats>("my-stats", getMyStats, { refreshInterval: 30_000 });

  const activeHrs  = ((stats?.active_seconds_today ?? 0) / 3600).toFixed(1);
  const topApp     = stats?.top_apps[0]?.app_name ?? "—";

  return (
    <div className="overflow-y-auto pr-1">
      {/* Welcome header */}
      <div className="mb-6">
        <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">
          Welcome back, {me.name}!
        </h1>
        <p className="text-sm text-gray-500 mt-0.5">
          <span className="font-medium text-blue-600">{me.role_name ?? "Employee"}</span> · {today}
        </p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <StatCard label="Active today"   value={`${activeHrs}h`} color="text-blue-600" />
        <StatCard label="Top app today"  value={topApp}          color="text-purple-600" />
        <StatCard label="My devices"     value={stats?.device_count ?? 0} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Top apps */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <AppWindow size={15} className="text-gray-400" />
            <p className="text-sm font-semibold text-gray-800">My Top Apps (today)</p>
          </div>
          <UsagePie items={stats?.top_apps ?? []} labelKey="app_name" valueKey="seconds" format={fmtSeconds} />
        </div>

        {/* Recent activity */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <Clock size={15} className="text-gray-400" />
            <p className="text-sm font-semibold text-gray-800">Recent Activity</p>
          </div>
          {(stats?.recent_events ?? []).length === 0 ? (
            <p className="text-sm text-gray-400 py-8 text-center">No activity recorded yet.</p>
          ) : (
            <div className="space-y-2">
              {(stats?.recent_events ?? []).map((e, i) => (
                <div key={i} className="flex items-start gap-3 text-sm">
                  <span className="text-gray-300 text-xs mt-0.5 shrink-0 w-14 text-right">
                    {e.timestamp_unix ? fmtTime(e.timestamp_unix) : "—"}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-gray-700 font-medium truncate">
                      {e.app_name ?? EVENT_LABEL[e.event_type] ?? e.event_type}
                    </p>
                    {e.window_title && (
                      <p className="text-xs text-gray-400 truncate">{e.window_title}</p>
                    )}
                    {e.url && !e.window_title && (
                      <p className="text-xs text-gray-400 truncate">{e.url}</p>
                    )}
                  </div>
                  {e.duration > 0 && (
                    <span className="text-xs text-gray-400 shrink-0">{fmtSeconds(e.duration)}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Page entry ────────────────────────────────────────────────────────────────

export default function HomePage() {
  const { me, loading, can } = useUser();

  if (loading || !me) return null;

  if (me.scope_type === "own") return <EmployeeHome me={me} />;
  if (!can("home", "view"))   return <AccessDenied label={`${me.role_name} Home`} />;
  return <AdminHome me={me} />;
}
