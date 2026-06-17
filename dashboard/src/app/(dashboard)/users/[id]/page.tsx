"use client";
import { use } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import {
  ArrowLeft, User as UserIcon, Monitor, Clock, CalendarDays, Laptop, AppWindow,
} from "lucide-react";
import { getUserActivityDetail, fmtSeconds } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { UserActivityDetail } from "@/types";

/* ── Helpers ─────────────────────────────────────────────────────────────── */

function fmtLastSeen(iso: string | null): string {
  if (!iso) return "Never";
  const diff = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 90)     return "Just now";
  if (diff < 3600)   return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)  return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function fmtDay(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  const today = new Date(); today.setHours(0, 0, 0, 0);
  if (d.getTime() === today.getTime()) return "Today";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function StatCard({ icon: Icon, label, value, sub }: {
  icon: React.ElementType; label: string; value: string; sub?: string;
}) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-2 text-gray-400 text-xs font-medium uppercase tracking-wide">
        <Icon size={13} />
        {label}
      </div>
      <p className="text-xl font-semibold text-gray-900 mt-1.5 tabular-nums">{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
    </div>
  );
}

/* ── Page ────────────────────────────────────────────────────────────────── */

export default function UserDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { loading, can } = useUser();

  // One request returns everything; refresh keeps online status + times live.
  const { data, error } = useSWR<UserActivityDetail>(
    ["user-activity", id],
    () => getUserActivityDetail(id),
    { refreshInterval: 30_000 },
  );

  if (loading) return null;
  if (!can("users", "view")) return <AccessDenied label="Users" />;

  if (error) {
    return (
      <div className="p-8 text-center">
        <p className="text-sm text-gray-500">Could not load this user.</p>
        <button onClick={() => router.push("/users")} className="text-brand-600 text-sm mt-2 hover:underline">
          Back to users
        </button>
      </div>
    );
  }

  const u = data?.user;
  const onlineDevices = data?.devices.filter(d => d.is_online).length ?? 0;
  const maxDay = Math.max(1, ...(data?.daily ?? []).map(d => d.active_seconds + d.idle_seconds));
  const maxApp = Math.max(1, ...(data?.top_apps ?? []).map(a => a.seconds));

  return (
    <div className="overflow-y-auto pr-1">
      {/* ── Back + header ── */}
      <Link href="/users" className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800 mb-3">
        <ArrowLeft size={15} /> Users
      </Link>

      <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-full bg-brand-50 flex items-center justify-center shrink-0">
            <UserIcon size={20} className="text-brand-600" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-gray-900 flex items-center gap-2.5 flex-wrap">
              {u ? u.name : <span className="inline-block w-40 h-6 bg-gray-100 rounded animate-pulse" />}
              {u && (
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                  u.is_online ? "bg-green-50 text-green-600" : "bg-gray-100 text-gray-500"
                }`}>
                  {u.is_online ? "● Online" : "○ Offline"}
                </span>
              )}
            </h1>
            <div className="flex items-center gap-3 mt-0.5 flex-wrap text-xs text-gray-500">
              {u?.email && <span>{u.email}</span>}
              {u?.role_name && (
                <span className="bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">{u.role_name}</span>
              )}
              {u?.is_online && u.current_app && (
                <span className="flex items-center gap-1 text-gray-400">
                  <AppWindow size={11} /> {u.current_app}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ── Summary cards ── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
        <StatCard icon={Clock}        label="Time today"  value={u ? fmtSeconds(u.today_seconds) : "—"} sub="active time" />
        <StatCard icon={CalendarDays} label="Last 7 days" value={u ? fmtSeconds(u.week_seconds) : "—"}
                  sub={u ? `~${fmtSeconds(Math.round(u.week_seconds / 7))} / day` : undefined} />
        <StatCard icon={Monitor}      label="Devices"     value={u ? String(u.device_count) : "—"}
                  sub={data ? `${onlineDevices} online` : undefined} />
        <StatCard icon={UserIcon}     label="Last seen"   value={u ? fmtLastSeen(u.last_seen) : "—"} />
      </div>

      <div className="grid lg:grid-cols-2 gap-4 mb-5">
        {/* ── Daily activity, last 7 days ── */}
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-4">
          <h2 className="text-sm font-semibold text-gray-800 mb-3">Daily activity (7 days)</h2>
          {data && data.daily.length === 0 && (
            <p className="text-sm text-gray-400 py-6 text-center">No activity recorded.</p>
          )}
          <div className="space-y-2.5">
            {(data?.daily ?? []).map(d => (
              <div key={d.date} className="flex items-center gap-3">
                <span className="text-xs text-gray-500 w-24 shrink-0">{fmtDay(d.date)}</span>
                <div className="flex-1 h-4 bg-gray-100 rounded overflow-hidden flex">
                  <div className="bg-brand-500 h-full" style={{ width: `${(d.active_seconds / maxDay) * 100}%` }}
                       title={`Active ${fmtSeconds(d.active_seconds)}`} />
                  <div className="bg-amber-300 h-full" style={{ width: `${(d.idle_seconds / maxDay) * 100}%` }}
                       title={`Idle ${fmtSeconds(d.idle_seconds)}`} />
                </div>
                <span className="text-xs text-gray-600 font-medium tabular-nums w-16 text-right shrink-0">
                  {fmtSeconds(d.active_seconds)}
                </span>
              </div>
            ))}
          </div>
          {data && data.daily.length > 0 && (
            <div className="flex items-center gap-4 mt-3 text-[11px] text-gray-400">
              <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-brand-500" /> Active</span>
              <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-amber-300" /> Idle</span>
            </div>
          )}
        </div>

        {/* ── Top apps, last 7 days ── */}
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-4">
          <h2 className="text-sm font-semibold text-gray-800 mb-3">Top apps (7 days)</h2>
          {data && data.top_apps.length === 0 && (
            <p className="text-sm text-gray-400 py-6 text-center">No app usage recorded.</p>
          )}
          <div className="space-y-2.5">
            {(data?.top_apps ?? []).map(a => (
              <div key={a.app_name} className="flex items-center gap-3">
                <span className="text-xs text-gray-600 w-32 truncate shrink-0" title={a.app_name}>{a.app_name}</span>
                <div className="flex-1 h-4 bg-gray-100 rounded overflow-hidden">
                  <div className="bg-brand-400 h-full" style={{ width: `${(a.seconds / maxApp) * 100}%` }} />
                </div>
                <span className="text-xs text-gray-600 font-medium tabular-nums w-16 text-right shrink-0">
                  {fmtSeconds(a.seconds)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── Devices table ── */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm mb-6">
        <div className="px-4 py-3 border-b border-gray-100">
          <h2 className="text-sm font-semibold text-gray-800">Devices</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[700px]">
            <thead>
              <tr className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                <th className="text-left px-4 py-3 font-semibold">Device</th>
                <th className="text-left px-4 py-3 font-semibold">Online</th>
                <th className="text-left px-4 py-3 font-semibold">Current app</th>
                <th className="text-left px-4 py-3 font-semibold">Time today</th>
                <th className="text-left px-4 py-3 font-semibold">Last 7 days</th>
                <th className="text-left px-4 py-3 font-semibold">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {(data?.devices ?? []).map(d => (
                <tr
                  key={d.id}
                  onClick={() => router.push(`/devices/${d.id}`)}
                  className="border-t border-gray-100 hover:bg-gray-50 cursor-pointer"
                  title="Open device"
                >
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-2 font-medium text-gray-800">
                      <Laptop size={14} className="text-gray-400 shrink-0" />
                      {d.hostname}
                    </span>
                    <p className="text-xs text-gray-400 ml-6">{d.platform}</p>
                  </td>
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-1.5 text-xs">
                      <span className={`w-2 h-2 rounded-full shrink-0 ${d.is_online ? "bg-green-500" : "bg-gray-300"}`} />
                      <span className={d.is_online ? "text-green-600 font-medium" : "text-gray-400"}>
                        {d.is_online ? "Online" : "Offline"}
                      </span>
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs truncate max-w-[180px]" title={d.current_app ?? ""}>
                    {d.current_app ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-gray-700 font-medium tabular-nums">{fmtSeconds(d.today_seconds)}</td>
                  <td className="px-4 py-3 text-gray-500 tabular-nums">{fmtSeconds(d.week_seconds)}</td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{fmtLastSeen(d.last_seen)}</td>
                </tr>
              ))}
              {data && data.devices.length === 0 && (
                <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400 text-sm">No devices assigned to this user.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
