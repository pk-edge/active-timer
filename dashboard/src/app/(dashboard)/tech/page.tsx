"use client";
import { useState, useMemo } from "react";
import useSWR from "swr";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  LineChart, Line, Legend, Cell,
} from "recharts";
import {
  Cpu, TrendingUp, TrendingDown, Users, Clock, Monitor,
  Globe, ChevronDown, RefreshCw, ArrowUpRight, ArrowDownRight, Minus,
} from "lucide-react";
import {
  getTechSummary, getCategoryAllocation, getAppTrends, getAppList, getAdoptionData,
  fmtSeconds,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type {
  TechSummary, CategoryAllocationItem, AppTrends, AppDetailItem, AdoptionData,
} from "@/types";

// ── Helpers ───────────────────────────────────────────────────────────────────

const PERIOD_OPTIONS = [
  { label: "Last 7 days",  days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "Last 60 days", days: 60 },
  { label: "Last 90 days", days: 90 },
];

const LINE_COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#ef4444", "#06b6d4", "#ec4899"];

function PctChangeBadge({ v }: { v: number }) {
  if (v === 0) return <span className="text-xs text-gray-400 flex items-center gap-0.5"><Minus size={11} />0%</span>;
  if (v > 0) return (
    <span className="text-xs text-green-600 flex items-center gap-0.5">
      <ArrowUpRight size={11} />+{v.toFixed(1)}%
    </span>
  );
  return (
    <span className="text-xs text-red-500 flex items-center gap-0.5">
      <ArrowDownRight size={11} />{v.toFixed(1)}%
    </span>
  );
}

function PeriodSelect({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div className="relative">
      <select
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        className="appearance-none bg-white border border-gray-200 rounded-lg pl-3 pr-8 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-300 shadow-sm"
      >
        {PERIOD_OPTIONS.map(o => (
          <option key={o.days} value={o.days}>{o.label}</option>
        ))}
      </select>
      <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
    </div>
  );
}

// ── Stat cards ────────────────────────────────────────────────────────────────

function SummaryCards({ data }: { data: TechSummary }) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-8 h-8 rounded-lg bg-blue-50 flex items-center justify-center">
            <Clock size={15} className="text-blue-600" />
          </div>
          <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">Total Hours</p>
        </div>
        <p className="text-2xl font-bold text-gray-900">{data.total_hours.toLocaleString()}</p>
        <p className="text-xs text-gray-400 mt-0.5">tracked this period</p>
      </div>

      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-8 h-8 rounded-lg bg-purple-50 flex items-center justify-center">
            <Monitor size={15} className="text-purple-600" />
          </div>
          <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">Applications</p>
        </div>
        <p className="text-2xl font-bold text-gray-900">{data.total_apps}</p>
        <p className="text-xs text-gray-400 mt-0.5">distinct apps used</p>
      </div>

      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-8 h-8 rounded-lg bg-green-50 flex items-center justify-center">
            <Users size={15} className="text-green-600" />
          </div>
          <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">Users Tracked</p>
        </div>
        <p className="text-2xl font-bold text-gray-900">{data.total_users}</p>
        <p className="text-xs text-gray-400 mt-0.5">active employees</p>
      </div>

      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center"
               style={{ backgroundColor: `${data.top_category_color}20` }}>
            <Cpu size={15} style={{ color: data.top_category_color }} />
          </div>
          <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">Top Category</p>
        </div>
        <p className="text-2xl font-bold text-gray-900 truncate">{data.top_category ?? "—"}</p>
        <p className="text-xs text-gray-400 mt-0.5 truncate">Top app: {data.top_app ?? "—"}</p>
      </div>
    </div>
  );
}

// ── Overview tab ──────────────────────────────────────────────────────────────

function OverviewTab({
  allocation, apps,
}: {
  allocation: CategoryAllocationItem[];
  apps: AppDetailItem[];
}) {
  const pieData = allocation.slice(0, 8);

  return (
    <div className="space-y-6">
      {/* Category allocation pie + legend */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <p className="text-sm font-semibold text-gray-800 mb-4">Category Allocation</p>
          <div className="flex items-center gap-4">
            <ResponsiveContainer width="55%" height={220}>
              <BarChart data={pieData} layout="vertical" barCategoryGap="20%">
                <XAxis type="number" tick={{ fontSize: 11, fill: "#9ca3af" }} axisLine={false} tickLine={false}
                       tickFormatter={v => `${v}%`} />
                <YAxis type="category" dataKey="category_name" width={90}
                       tick={{ fontSize: 11, fill: "#6b7280" }} axisLine={false} tickLine={false} />
                <Tooltip
                  formatter={(v: number) => [`${v}%`, "Allocation"]}
                  contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }}
                />
                <Bar dataKey="pct" radius={[0, 4, 4, 0]}>
                  {pieData.map((entry, i) => (
                    <Cell key={i} fill={entry.color} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            <div className="flex-1 space-y-2 min-w-0">
              {pieData.map(item => (
                <div key={item.category_name} className="flex items-center gap-2 text-xs">
                  <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: item.color }} />
                  <span className="text-gray-600 truncate flex-1">{item.category_name}</span>
                  <span className="text-gray-500 font-medium shrink-0">{item.pct.toFixed(1)}%</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Hours by category */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <p className="text-sm font-semibold text-gray-800 mb-4">Hours by Category</p>
          <div className="space-y-3">
            {allocation.map(item => (
              <div key={item.category_name}>
                <div className="flex items-center justify-between text-xs mb-1">
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full" style={{ background: item.color }} />
                    <span className="text-gray-700">{item.category_name}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <PctChangeBadge v={item.pct_change} />
                    <span className="text-gray-500">{item.hours.toFixed(1)}h</span>
                  </div>
                </div>
                <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                  <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(item.pct, 100)}%`, background: item.color }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* App list */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100 flex items-center gap-2">
          <Globe size={15} className="text-gray-400" />
          <p className="text-sm font-semibold text-gray-800">Top Applications</p>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 text-gray-400 text-xs uppercase tracking-wide">
              <th className="text-left px-5 py-2.5 font-semibold">Application</th>
              <th className="text-left px-5 py-2.5 font-semibold">Category</th>
              <th className="text-left px-5 py-2.5 font-semibold">Hours</th>
              <th className="text-left px-5 py-2.5 font-semibold">Allocation</th>
              <th className="text-left px-5 py-2.5 font-semibold">Users</th>
            </tr>
          </thead>
          <tbody>
            {apps.slice(0, 20).map(app => (
              <tr key={app.app_name} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-5 py-3 font-medium text-gray-800">{app.app_name}</td>
                <td className="px-5 py-3">
                  <span className="inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full"
                        style={{ background: `${app.color}18`, color: app.color }}>
                    <span className="w-1.5 h-1.5 rounded-full" style={{ background: app.color }} />
                    {app.category_name}
                  </span>
                </td>
                <td className="px-5 py-3 text-gray-600">{app.hours.toFixed(1)}h</td>
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <div className="w-16 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                      <div className="h-full rounded-full" style={{ width: `${Math.min(app.pct, 100)}%`, background: app.color }} />
                    </div>
                    <span className="text-xs text-gray-500">{app.pct.toFixed(1)}%</span>
                  </div>
                </td>
                <td className="px-5 py-3 text-gray-500">{app.user_count}</td>
              </tr>
            ))}
            {apps.length === 0 && (
              <tr><td colSpan={5} className="px-5 py-10 text-center text-gray-400">No data yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Top Changes tab ───────────────────────────────────────────────────────────

function TopChangesTab({ allocation, trends }: { allocation: CategoryAllocationItem[]; trends: AppTrends }) {
  const chartData = useMemo(
    () => [...allocation].sort((a, b) => Math.abs(b.pct_change) - Math.abs(a.pct_change)).slice(0, 10),
    [allocation],
  );

  return (
    <div className="space-y-6">
      {/* Category allocation change chart */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <p className="text-sm font-semibold text-gray-800 mb-1">Top Changes in Category Allocation</p>
        <p className="text-xs text-gray-400 mb-4">Percentage point change vs comparison period</p>
        <div className="overflow-x-auto">
          <div style={{ minWidth: 500 }}>
            <ResponsiveContainer width="100%" height={280}>
              <BarChart data={chartData} layout="vertical" barCategoryGap="25%" margin={{ left: 8 }}>
                <XAxis type="number" tick={{ fontSize: 11, fill: "#9ca3af" }} axisLine={false} tickLine={false}
                       tickFormatter={v => `${v > 0 ? "+" : ""}${v}%`} />
                <YAxis type="category" dataKey="category_name" width={110}
                       tick={{ fontSize: 11, fill: "#6b7280" }} axisLine={false} tickLine={false} />
                <Tooltip
                  formatter={(v: number) => [`${v > 0 ? "+" : ""}${v.toFixed(1)}%`, "Change"]}
                  contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }}
                />
                <Bar dataKey="pct_change" radius={[0, 4, 4, 0]}>
                  {chartData.map((entry, i) => (
                    <Cell key={i} fill={entry.pct_change >= 0 ? "#3b82f6" : "#ef4444"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* Category details table */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100">
          <p className="text-sm font-semibold text-gray-800">Changes in Category Allocation</p>
          <p className="text-xs text-gray-400 mt-0.5">Identify top changes to understand technology usage trends.</p>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 text-gray-400 text-xs uppercase tracking-wide">
              <th className="text-left px-5 py-2.5 font-semibold">Category</th>
              <th className="text-left px-5 py-2.5 font-semibold">Allocation % Chg</th>
              <th className="text-left px-5 py-2.5 font-semibold">Allocation %</th>
              <th className="text-left px-5 py-2.5 font-semibold">Hrs</th>
              <th className="text-left px-5 py-2.5 font-semibold">Users Chg</th>
            </tr>
          </thead>
          <tbody>
            {[...allocation].sort((a, b) => Math.abs(b.pct_change) - Math.abs(a.pct_change)).map(item => (
              <tr key={item.category_name} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-sm" style={{ background: item.color }} />
                    <span className="font-medium text-gray-800">{item.category_name}</span>
                  </div>
                </td>
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <PctChangeBadge v={item.pct_change} />
                    <div className="w-20 h-2 bg-gray-100 rounded-full overflow-hidden">
                      <div className="h-full rounded-full"
                           style={{
                             width: `${Math.min(Math.abs(item.pct_change) * 10, 100)}%`,
                             background: item.pct_change >= 0 ? "#3b82f6" : "#ef4444",
                           }} />
                    </div>
                  </div>
                </td>
                <td className="px-5 py-3 text-gray-600">{item.pct.toFixed(1)}%</td>
                <td className="px-5 py-3 text-gray-600">{item.hours.toFixed(1)}</td>
                <td className="px-5 py-3">
                  <PctChangeBadge v={item.user_change} />
                </td>
              </tr>
            ))}
            {allocation.length === 0 && (
              <tr><td colSpan={5} className="px-5 py-10 text-center text-gray-400">No data yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Trending up/down */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <TrendingTable title="Applications/Sites Trending Up" icon={TrendingUp} items={trends.trending_up} up />
        <TrendingTable title="Applications/Sites Trending Down" icon={TrendingDown} items={trends.trending_down} up={false} />
      </div>
    </div>
  );
}

function TrendingTable({
  title, icon: Icon, items, up,
}: {
  title: string;
  icon: React.ElementType;
  items: AppTrends["trending_up"];
  up: boolean;
}) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-100 flex items-center gap-2">
        <Icon size={15} className={up ? "text-green-500" : "text-red-400"} />
        <p className="text-sm font-semibold text-gray-800">{title}</p>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-gray-50 text-gray-400 text-xs uppercase tracking-wide">
            <th className="text-left px-4 py-2 font-semibold">App / Site</th>
            <th className="text-left px-4 py-2 font-semibold">Alloc % Chg</th>
            <th className="text-right px-4 py-2 font-semibold">Hrs</th>
            <th className="text-right px-4 py-2 font-semibold">Users</th>
          </tr>
        </thead>
        <tbody>
          {items.map(app => (
            <tr key={app.app_name} className="border-t border-gray-100 hover:bg-gray-50">
              <td className="px-4 py-2.5">
                <p className="font-medium text-gray-800 truncate max-w-[120px]">{app.app_name}</p>
                <p className="text-xs text-gray-400">{app.category_name}</p>
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <PctChangeBadge v={app.pct_change} />
                  <div className="w-16 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div className="h-full rounded-full"
                         style={{
                           width: `${Math.min(Math.abs(app.pct_change) * 20, 100)}%`,
                           background: app.color,
                         }} />
                  </div>
                </div>
              </td>
              <td className="px-4 py-2.5 text-right text-gray-600">{app.hours.toFixed(1)}</td>
              <td className="px-4 py-2.5 text-right">
                <PctChangeBadge v={app.user_change} />
              </td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr><td colSpan={4} className="px-4 py-8 text-center text-gray-400">No changes in this period.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ── Adoption tab ──────────────────────────────────────────────────────────────

function AdoptionTab({ data }: { data: AdoptionData }) {
  if (!data.apps.length) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-10 shadow-sm flex items-center justify-center">
        <p className="text-sm text-gray-400">No adoption data for this period.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <p className="text-sm font-semibold text-gray-800 mb-1">Application Adoption Over Time</p>
        <p className="text-xs text-gray-400 mb-4">Daily unique users per top application</p>
        <ResponsiveContainer width="100%" height={320}>
          <LineChart data={data.series} margin={{ top: 4, right: 16, bottom: 4, left: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#9ca3af" }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: "#9ca3af" }} axisLine={false} tickLine={false} width={28} />
            <Tooltip
              contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }}
            />
            <Legend wrapperStyle={{ fontSize: 12, paddingTop: 12 }} />
            {data.apps.map((app, i) => (
              <Line
                key={app}
                type="monotone"
                dataKey={app}
                stroke={LINE_COLORS[i % LINE_COLORS.length]}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

type Tab = "overview" | "top-changes" | "adoption";

export default function TechUtilizationPage() {
  const { loading, can } = useUser();
  const [tab, setTab]           = useState<Tab>("overview");
  const [days, setDays]         = useState(30);
  const [compareDays]           = useState(30);

  const { data: summary, mutate: mutateSummary } =
    useSWR<TechSummary>(["tech-summary", days], () => getTechSummary(days));
  const { data: allocation, mutate: mutateAlloc } =
    useSWR<CategoryAllocationItem[]>(["tech-alloc", days, compareDays], () => getCategoryAllocation(days, compareDays));
  const { data: trends, mutate: mutateTrends } =
    useSWR<AppTrends>(["tech-trends", days, compareDays], () => getAppTrends(days, compareDays, 10));
  const { data: apps, mutate: mutateApps } =
    useSWR<AppDetailItem[]>(["tech-apps", days], () => getAppList(days, undefined, 50));
  const { data: adoption, mutate: mutateAdoption } =
    useSWR<AdoptionData>(["tech-adoption", days], () => getAdoptionData(days, 5));

  function refresh() {
    mutateSummary(); mutateAlloc(); mutateTrends(); mutateApps(); mutateAdoption();
  }

  if (loading) return null;
  if (!can("tech", "view")) return <AccessDenied label="Technology Utilization" />;

  const TABS: { key: Tab; label: string }[] = [
    { key: "overview",    label: "Overview" },
    { key: "top-changes", label: "Top Changes" },
    { key: "adoption",    label: "Adoption" },
  ];

  return (
    <div className="overflow-y-auto pr-1">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900">Technology Utilization</h1>
          <p className="text-sm text-gray-500 mt-0.5">Track application usage and category allocation across your organization</p>
        </div>
        <div className="flex items-center gap-2">
          <PeriodSelect value={days} onChange={setDays} />
          <button
            onClick={refresh}
            className="flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm"
          >
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
      </div>

      {/* Summary cards */}
      {summary && <SummaryCards data={summary} />}

      {/* Tabs */}
      <div className="flex items-center gap-1 bg-gray-100 rounded-lg p-1 mb-6 w-fit">
        {TABS.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-4 py-1.5 rounded-md text-sm font-medium transition ${
              tab === t.key ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {tab === "overview" && (
        <OverviewTab allocation={allocation ?? []} apps={apps ?? []} />
      )}
      {tab === "top-changes" && (
        <TopChangesTab allocation={allocation ?? []} trends={trends ?? { trending_up: [], trending_down: [] }} />
      )}
      {tab === "adoption" && (
        <AdoptionTab data={adoption ?? { apps: [], series: [] }} />
      )}
    </div>
  );
}
