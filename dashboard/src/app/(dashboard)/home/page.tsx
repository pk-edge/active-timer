"use client";
import { useState, useMemo } from "react";
import Link from "next/link";
import useSWR from "swr";
import {
  Monitor, BarChart2, Users2, DownloadCloud,
  Wifi, Coffee, WifiOff, Clock, Activity as ActivityIcon,
  AppWindow, ChevronDown, CalendarDays, Search, X,
} from "lucide-react";
import { format, subDays, startOfMonth, startOfYear, subMonths } from "date-fns";
import { getOverview, getHomeAppUsage, getMyStats, fmtSeconds, fmtTime } from "@/lib/api";
import PageLoader from "@/components/PageLoader";
import PageSkeleton from "@/components/PageSkeleton";
import GroupPicker from "@/components/GroupPicker";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useRealtimeStream } from "@/hooks/useRealtimeStream";
import { smartMatch } from "@/lib/smartSearch";
import { AccessDenied } from "@/components/AccessDenied";
import { StatusDonut, UsagePie } from "@/components/charts";
import type { OverviewStats, AppUsage, MyStats } from "@/types";

// ── Stat period filter ────────────────────────────────────────────────────────

const TODAY = () => format(new Date(), "yyyy-MM-dd");

const STAT_PRESETS = [
  { label: "Today",         getSince: () => TODAY(),                                       getUntil: () => TODAY() },
  { label: "Yesterday",     getSince: () => format(subDays(new Date(), 1), "yyyy-MM-dd"), getUntil: () => format(subDays(new Date(), 1), "yyyy-MM-dd") },
  { label: "Last 7 Days",   getSince: () => format(subDays(new Date(), 6), "yyyy-MM-dd"), getUntil: () => TODAY() },
  { label: "Last 30 Days",  getSince: () => format(subDays(new Date(), 29), "yyyy-MM-dd"), getUntil: () => TODAY() },
  { label: "This Month",    getSince: () => format(startOfMonth(new Date()), "yyyy-MM-dd"), getUntil: () => TODAY() },
  { label: "Last Month",    getSince: () => format(startOfMonth(subMonths(new Date(), 1)), "yyyy-MM-dd"), getUntil: () => format(new Date(new Date().getFullYear(), new Date().getMonth(), 0), "yyyy-MM-dd") },
  { label: "This Year",     getSince: () => format(startOfYear(new Date()), "yyyy-MM-dd"), getUntil: () => TODAY() },
  { label: "Custom",        getSince: () => TODAY(), getUntil: () => TODAY() },
];

function StatPeriodFilter({
  dateRange,
  onRangeChange,
}: {
  dateRange: { since: string; until: string };
  onRangeChange: (range: { since: string; until: string }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [customMode, setCustomMode] = useState(false);
  const [localSince, setLocalSince] = useState(dateRange.since);
  const [localUntil, setLocalUntil] = useState(dateRange.until);

  const activePreset = STAT_PRESETS.find(
    p => p.label !== "Custom" && p.getSince() === dateRange.since && p.getUntil() === dateRange.until
  );
  const displayLabel = activePreset?.label ?? "Custom";
  const displayRange = dateRange.since === dateRange.until ? dateRange.since : `${dateRange.since} – ${dateRange.until}`;

  function applyPreset(p: typeof STAT_PRESETS[number]) {
    if (p.label === "Custom") {
      setLocalSince(dateRange.since);
      setLocalUntil(dateRange.until);
      setCustomMode(true);
      return;
    }
    setCustomMode(false);
    onRangeChange({ since: p.getSince(), until: p.getUntil() });
    setOpen(false);
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(x => !x)}
        className="flex items-center gap-2 bg-white border border-gray-200 rounded-lg px-3 py-1.5 text-sm text-gray-700 hover:border-brand-300 shadow-sm transition"
      >
        <CalendarDays size={14} className="text-gray-600 shrink-0" />
        <span className="font-medium">{displayLabel}</span>
        <span className="text-gray-600 text-xs hidden sm:inline">{displayRange}</span>
        <ChevronDown size={12} className={`text-gray-600 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-full mt-1.5 z-40 bg-white border border-gray-200 rounded-xl shadow-xl w-72">
            <div className="p-3 border-b border-gray-100">
              <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-2">Period</p>
              <div className="grid grid-cols-2 gap-1">
                {STAT_PRESETS.map(p => (
                  <button
                    key={p.label}
                    onClick={() => applyPreset(p)}
                    className={`text-left px-3 py-1.5 rounded-lg text-sm transition ${
                      displayLabel === p.label
                        ? "bg-brand-50 text-brand-700 font-medium"
                        : "text-gray-600 hover:bg-gray-50"
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
            {(customMode || displayLabel === "Custom") && (
              <div className="p-3">
                <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-2">Custom Range</p>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">From</label>
                    <input type="date" value={localSince} max={localUntil}
                           onChange={e => setLocalSince(e.target.value)}
                           className="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">To</label>
                    <input type="date" value={localUntil} min={localSince} max={TODAY()}
                           onChange={e => setLocalUntil(e.target.value)}
                           className="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
                  </div>
                </div>
                <button onClick={() => { onRangeChange({ since: localSince, until: localUntil }); setOpen(false); }}
                        className="mt-2 w-full bg-brand-600 text-white text-sm py-1.5 rounded-lg hover:bg-brand-700 transition font-medium">
                  Apply
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

const STATUS_STYLE: Record<string, string> = {
  active:   "bg-green-50 text-green-700 border-green-200",
  passive:  "bg-amber-50 text-amber-700 border-amber-200",
  inactive: "bg-gray-100 text-gray-600 border-gray-200",
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
      {sub && <p className="text-xs text-gray-600 mt-0.5">{sub}</p>}
      <p className="text-xs text-gray-600 mt-1">{label}</p>
    </div>
  );
}

// ── Admin / Manager home ──────────────────────────────────────────────────────

function AdminHome() {
  const { can } = useUser();
  const todayStr = format(new Date(), "yyyy-MM-dd");

  const [dateRange, setDateRange] = useState({ since: todayStr, until: todayStr });
  const [groupId, setGroupId]     = useState("");
  const [empSearch, setEmpSearch] = useState("");

  const isToday = dateRange.since === todayStr && dateRange.until === todayStr;
  const activeLabel = isToday
    ? "Active today"
    : dateRange.since === dateRange.until ? `Active on ${dateRange.since}` : "Active in period";
  const avgLabel = isToday
    ? "Avg active / device"
    : "Avg active / device (period)";

  const { data: stats } = useSWR<OverviewStats>(
    ["home-overview", dateRange.since, dateRange.until, groupId],
    () => getOverview(groupId || undefined, 7, dateRange.since, dateRange.until),
    { refreshInterval: 300_000, dedupingInterval: 10_000 },
  );

  const realtime = useRealtimeStream(groupId || undefined);

  // Fire in parallel with stats — both requests start on mount so the pie
  // chart and stat cards load concurrently with no waterfall.
  const { data: apps } = useSWR<AppUsage[]>(
    ["home-apps", groupId],
    () => getHomeAppUsage(groupId || undefined, 1),
    { refreshInterval: 300_000, dedupingInterval: 10_000 },
  );

  const debouncedEmpSearch = useDebouncedValue(empSearch, 200);
  const filteredRealtime = useMemo(() => {
    if (!debouncedEmpSearch.trim()) return realtime;
    return realtime.filter(d => smartMatch(debouncedEmpSearch, d.employee_name, d.hostname));
  }, [realtime, debouncedEmpSearch]);

  const s = stats?.status ?? { active: 0, passive: 0, inactive: 0 };
  const avgSecs = stats?.avg_active_seconds_today ?? 0;
  const avgHrs  = (avgSecs / 3600).toFixed(1);
  const avgMins = Math.round(avgSecs / 60);
  const avgDisplay = avgSecs < 3600 ? `${avgMins}m` : `${avgHrs}h`;

  // Online right now = same logic as /v1/devices is_online: any device seen within
  // the cutoff window, regardless of whether its latest event was lock/idle_start.
  // Use the live SSE count (active + passive) when available; fall back to overview.
  const onlineNow = realtime.length > 0
    ? realtime.filter(d => d.status !== "inactive").length
    : s.active + s.passive;

  const QUICK_ACTIONS = [
    { href: "/devices",  label: "Devices",  icon: Monitor,      module: "devices" },
    { href: "/reports",  label: "Reports",  icon: BarChart2,    module: "reports" },
    { href: "/users",    label: "Users",    icon: Users2,        module: "users" },
    { href: "/agents",   label: "Agents",   icon: DownloadCloud, module: "agents" },
  ];


  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      {/* Quick actions */}
      <div className="flex flex-wrap gap-3 mb-6">
        {QUICK_ACTIONS.filter(a => can(a.module, "view")).map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 hover:border-brand-400 hover:text-brand-600 shadow-sm transition"
          >
            <Icon size={15} />
            {label}
          </Link>
        ))}
      </div>

      {/* ── Filters row: period + group + user search ── */}
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide shrink-0">Stats for</p>
        <StatPeriodFilter
          dateRange={dateRange}
          onRangeChange={setDateRange}
        />

        {/* Group filter — includes subgroups when a parent is selected */}
        <GroupPicker value={groupId} onChange={setGroupId} emptyLabel="All groups" />

        {groupId && (
          <button
            onClick={() => setGroupId("")}
            className="text-xs text-gray-600 hover:text-gray-600 flex items-center gap-1"
          >
            <X size={12} /> Clear group
          </button>
        )}
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <StatCard label="Total devices"   value={stats?.total_devices ?? 0} />
        <StatCard label={activeLabel}     value={stats?.active_today ?? 0}  color="text-green-600" />
        <StatCard label="Online right now" value={onlineNow}               color="text-brand-600" />
        <StatCard
          label={avgLabel}
          value={avgDisplay}
          sub="working time only"
          color="text-purple-600"
        />
      </div>

      {/* Donut + Top Apps */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <ActivityIcon size={15} className="text-gray-600" />
            <p className="text-sm font-semibold text-gray-800">Live Activity Status</p>
          </div>
          <div className="grid grid-cols-2 gap-4 items-center">
            <StatusDonut active={s.active} passive={s.passive} inactive={s.inactive} />
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <Wifi size={18} className="text-green-500" />
                <div><p className="text-xl font-bold text-gray-900 leading-none">{s.active}</p><p className="text-xs text-gray-600 mt-0.5">Active now</p></div>
              </div>
              <div className="flex items-center gap-3">
                <Coffee size={18} className="text-amber-500" />
                <div><p className="text-xl font-bold text-gray-900 leading-none">{s.passive}</p><p className="text-xs text-gray-600 mt-0.5">Passive now</p></div>
              </div>
              <div className="flex items-center gap-3">
                <WifiOff size={18} className="text-gray-600" />
                <div><p className="text-xl font-bold text-gray-900 leading-none">{s.inactive}</p><p className="text-xs text-gray-600 mt-0.5">Inactive now</p></div>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <AppWindow size={15} className="text-gray-600" />
            <p className="text-sm font-semibold text-gray-800">Top Applications (today)</p>
          </div>
          <UsagePie items={apps ?? []} labelKey="app_name" valueKey="seconds" format={fmtSeconds} />
        </div>
      </div>

      {/* Live Device Feed — with scroller */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <div className="flex flex-wrap items-center gap-3 px-5 py-3 border-b border-gray-100">
          <Clock size={15} className="text-gray-600 shrink-0" />
          <p className="text-sm font-semibold text-gray-800">Live Device Feed</p>
          <span className="text-xs text-gray-600">{filteredRealtime.length} of {realtime.length} devices</span>

          {/* User / employee search */}
          <div className="ml-auto relative w-52">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
            <input
              type="text"
              value={empSearch}
              onChange={e => setEmpSearch(e.target.value)}
              placeholder="Search user or device…"
              className="w-full pl-7 pr-7 py-1.5 text-xs bg-gray-50 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-300 transition"
            />
            {empSearch && (
              <button onClick={() => setEmpSearch("")} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600">
                <X size={12} />
              </button>
            )}
          </div>
        </div>

        {/* Scrollable table — max-h so it doesn't expand infinitely */}
        <div className="overflow-auto max-h-72">
          <table className="w-full text-sm min-w-[640px]">
            <thead className="sticky top-0 z-10">
              <tr className="bg-gray-50 text-gray-600 text-xs uppercase tracking-wide">
                <th className="text-left px-5 py-2.5 font-semibold">Employee</th>
                <th className="text-left px-5 py-2.5 font-semibold">Device</th>
                <th className="text-left px-5 py-2.5 font-semibold">Status</th>
                <th className="text-left px-5 py-2.5 font-semibold">Current app</th>
                <th className="text-left px-5 py-2.5 font-semibold">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {filteredRealtime.map(d => (
                <tr key={d.device_id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-5 py-3 font-medium text-gray-800">
                    <Link href={`/devices/${d.device_id}`} className="hover:text-brand-600 transition">
                      {d.employee_name ?? "—"}
                    </Link>
                  </td>
                  <td className="px-5 py-3 text-gray-600">{d.hostname}</td>
                  <td className="px-5 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded-full border capitalize ${STATUS_STYLE[d.status]}`}>
                      {d.status}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-gray-600 max-w-[180px] truncate">
                    {d.current_app ?? <span className="text-gray-500">—</span>}
                  </td>
                  <td className="px-5 py-3 text-gray-600 text-xs">
                    {d.last_seen_unix ? fmtTime(d.last_seen_unix) : "—"}
                  </td>
                </tr>
              ))}
              {filteredRealtime.length === 0 && (
                <tr><td colSpan={5} className="px-5 py-10 text-center text-gray-600">
                  {empSearch ? "No devices match your search." : "No devices yet."}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── Employee home ─────────────────────────────────────────────────────────────

function EmployeeHome() {
  const { data: stats, isLoading } = useSWR<MyStats>("my-stats", getMyStats, { refreshInterval: 30_000 });

  const activeHrs  = ((stats?.active_seconds_today ?? 0) / 3600).toFixed(1);
  const topApp     = stats?.top_apps[0]?.app_name ?? "—";

  if (isLoading && !stats) return <PageSkeleton variant="dashboard" cards={3} />;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      {/* Stat cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <StatCard label="Active today"   value={`${activeHrs}h`} color="text-brand-600" />
        <StatCard label="Top app today"  value={topApp}          color="text-purple-600" />
        <StatCard label="My devices"     value={stats?.device_count ?? 0} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Top apps */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <AppWindow size={15} className="text-gray-600" />
            <p className="text-sm font-semibold text-gray-800">My Top Apps (today)</p>
          </div>
          <UsagePie items={stats?.top_apps ?? []} labelKey="app_name" valueKey="seconds" format={fmtSeconds} />
        </div>

        {/* Recent activity */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <Clock size={15} className="text-gray-600" />
            <p className="text-sm font-semibold text-gray-800">Recent Activity</p>
          </div>
          {(stats?.recent_events ?? []).length === 0 ? (
            <p className="text-sm text-gray-600 py-8 text-center">No activity recorded yet.</p>
          ) : (
            <div className="space-y-2">
              {(stats?.recent_events ?? []).map((e, i) => (
                <div key={i} className="flex items-start gap-3 text-sm">
                  <span className="text-gray-500 text-xs mt-0.5 shrink-0 w-14 text-right">
                    {e.timestamp_unix ? fmtTime(e.timestamp_unix) : "—"}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-gray-700 font-medium truncate">
                      {e.app_name ?? EVENT_LABEL[e.event_type] ?? e.event_type}
                    </p>
                    {e.window_title && (
                      <p className="text-xs text-gray-600 truncate">{e.window_title}</p>
                    )}
                    {e.url && !e.window_title && (
                      <p className="text-xs text-gray-600 truncate">{e.url}</p>
                    )}
                  </div>
                  {e.duration > 0 && (
                    <span className="text-xs text-gray-600 shrink-0">{fmtSeconds(e.duration)}</span>
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

  if (loading || !me) return <PageLoader />;

  if (me.scope_type === "own") return <EmployeeHome />;
  if (!can("home", "view"))   return <AccessDenied label={`${me.role_name} Home`} />;
  return <AdminHome />;
}
