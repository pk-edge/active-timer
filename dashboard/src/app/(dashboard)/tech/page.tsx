"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useMemo } from "react";
import useSWR from "swr";
import {
  PieChart, Pie, Cell, Tooltip, ResponsiveContainer,
  BarChart, Bar, XAxis, YAxis, LabelList,
  LineChart, Line, Legend } from "recharts";
import {
  Cpu, TrendingUp, TrendingDown, Users, Clock, Monitor, Globe,
  ChevronDown, ArrowUpRight, ArrowDownRight, Minus, Info } from "lucide-react";
import {
  getTechSummary, getCategoryAllocation, getAppTrends, getAppList,
  getAdoptionData, getTechCategories, fmtSeconds } from "@/lib/api";
import DateRangePicker, { type DateRange } from "@/components/DateRangePicker";
import GroupPicker from "@/components/GroupPicker";
import { format } from "date-fns";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import PageSkeleton from "@/components/PageSkeleton";
import type {
  TechSummary, CategoryAllocationItem, AppTrends, AppDetailItem, AdoptionData, AppCategory } from "@/types";

// ── Helpers ───────────────────────────────────────────────────────────────────

const LINE_COLORS = ["#4a0f70","#22c55e","#f59e0b","#a855f7","#ef4444","#06b6d4","#ec4899"];
const RADIAN = Math.PI / 180;

function PctBadge({ v }: { v: number }) {
  if (v === 0) return <span className="inline-flex items-center gap-0.5 text-xs text-gray-600"><Minus size={10} />0%</span>;
  if (v > 0)   return <span className="inline-flex items-center gap-0.5 text-xs text-green-600"><ArrowUpRight size={10} />+{v.toFixed(1)}%</span>;
  return              <span className="inline-flex items-center gap-0.5 text-xs text-red-500"><ArrowDownRight size={10} />{v.toFixed(1)}%</span>;
}

function FilterChip({
  label, value, options, onChange }: { label: string; value: string; options: { label: string; value: string }[]; onChange: (v: string) => void }) {
  const selected = options.find(o => o.value === value);
  return (
    <div className="relative">
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-7 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 cursor-pointer"
      >
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
    </div>
  );
}

// Skeleton shown while a tab's data lazy-loads (tabs fetch only when selected).
function TabSkeleton() {
  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-5 animate-pulse">
      <div className="h-4 w-56 bg-gray-200 rounded mb-4" />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="h-[280px] bg-gray-50 rounded-lg" />
        <div className="space-y-2 py-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-5 bg-gray-100 rounded" style={{ width: `${90 - i * 7}%` }} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Donut chart with outer labels ─────────────────────────────────────────────

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function DonutLabel({ cx, cy, midAngle, outerRadius, percent, name }: any) {
  if (percent < 0.025) return null;
  const radius = outerRadius + 38;
  const x = cx + radius * Math.cos(-midAngle * RADIAN);
  const y = cy + radius * Math.sin(-midAngle * RADIAN);
  return (
    <text x={x} y={y} fill="#374151" textAnchor={x > cx ? "start" : "end"}
          dominantBaseline="central" style={{ fontSize: 10, fontWeight: 500 }}>
      {`${name} - ${(percent * 100).toFixed(1)}%`}
    </text>
  );
}

// ── Overview tab ──────────────────────────────────────────────────────────────

function OverviewTab({
  allocation, apps, categoryId }: {
  allocation: CategoryAllocationItem[];
  apps:       AppDetailItem[];
  categoryId: string;
}) {
  // Donut data — all categories
  const donutData = useMemo(
    () => allocation.map(a => ({ name: a.category_name, value: a.pct, color: a.color })),
    [allocation],
  );

  // Horizontal bar chart — top 20 apps, sorted by pct desc
  const barData = useMemo(
    () => apps.slice(0, 20).map(a => ({
      name:     a.app_name.length > 22 ? a.app_name.slice(0, 20) + "…" : a.app_name,
      fullName: a.app_name,
      pct:      a.pct,
      color:    a.color,
      users:    a.user_count })),
    [apps],
  );

  const maxPct = barData[0]?.pct ?? 1;

  return (
    <div className="flex flex-col gap-2">
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        {/* Section title */}
        <div className="px-5 pt-4 pb-3 border-b border-gray-100">
          <div className="flex items-center gap-1.5">
            <p className="text-sm font-semibold text-gray-800">
              Usage by Category and Top Applications/Sites
            </p>
            <Info size={13} className="text-gray-600" />
          </div>
          <p className="text-xs text-gray-600 mt-0.5">
            Identify your organisation&apos;s activity allocation (% of total duration) by category and top applications/sites.
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-gray-100">
          {/* LEFT — Donut */}
          <div className="p-5">
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-3">
              Category Allocation %
            </p>
            {donutData.length === 0 ? (
              <div className="h-[280px] flex items-center justify-center text-sm text-gray-600">No data yet.</div>
            ) : (
              <ResponsiveContainer width="100%" height={300}>
                <PieChart margin={{ top: 20, right: 60, bottom: 20, left: 60 }}>
                  <Pie
                    data={donutData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    innerRadius={72}
                    outerRadius={105}
                    paddingAngle={1}
                    labelLine={false}
                    label={DonutLabel}
                  >
                    {donutData.map((d, i) => (
                      <Cell
                        key={i}
                        fill={d.color}
                        stroke={categoryId && allocation[i]?.category_id !== categoryId ? "#fff" : d.color}
                        opacity={categoryId && allocation[i]?.category_id !== categoryId ? 0.35 : 1}
                      />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(v: number) => [`${v.toFixed(1)}%`, "Allocation"]}
                    contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 11 }}
                  />
                </PieChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* RIGHT — Horizontal bar chart */}
          <div className="p-5">
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-3">
              Top Applications/Sites by Allocation % based on Activity Date
            </p>
            {barData.length === 0 ? (
              <div className="h-[280px] flex items-center justify-center text-sm text-gray-600">No data yet.</div>
            ) : (
              <div className="space-y-1.5 max-h-[380px] overflow-y-auto pr-1">
                {barData.map(app => (
                  <div key={app.fullName} className="flex items-center gap-2 group">
                    {/* App name */}
                    <p className="text-xs text-gray-700 w-36 shrink-0 truncate" title={app.fullName}>
                      {app.name}
                    </p>
                    {/* Bar + user dot */}
                    <div className="flex-1 flex items-center gap-1.5">
                      <div className="flex-1 h-5 bg-gray-100 rounded-sm overflow-hidden relative">
                        <div
                          className="h-full rounded-sm transition-all"
                          style={{
                            width: `${(app.pct / maxPct) * 100}%`,
                            background: app.color }}
                        />
                        {app.users > 0 && (
                          <div
                            className="absolute top-1/2 -translate-y-1/2 w-4 h-4 rounded-full bg-white border-2 flex items-center justify-center text-[8px] font-bold shadow-sm"
                            style={{
                              left: `calc(${(app.pct / maxPct) * 100}% - 8px)`,
                              borderColor: app.color,
                              color: app.color }}
                          >
                            {app.users > 9 ? "9+" : app.users}
                          </div>
                        )}
                      </div>
                      <span className="text-xs text-gray-600 w-10 text-right tabular-nums shrink-0">
                        {app.pct.toFixed(1)}%
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Top Changes tab ───────────────────────────────────────────────────────────

function TopChangesTab({ allocation, trends }: { allocation: CategoryAllocationItem[]; trends: AppTrends }) {
  const sorted = useMemo(
    () => [...allocation].sort((a, b) => Math.abs(b.pct_change) - Math.abs(a.pct_change)).slice(0, 10),
    [allocation],
  );

  return (
    <div className="space-y-6">
      {/* Category change bar chart */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <p className="text-sm font-semibold text-gray-800 mb-0.5">Top Changes in Category Allocation</p>
        <p className="text-xs text-gray-600 mb-4">Percentage point change vs comparison period</p>
        <ResponsiveContainer width="100%" height={Math.max(sorted.length * 36, 200)}>
          <BarChart data={sorted} layout="vertical" barCategoryGap="30%" margin={{ left: 8, right: 48 }}>
            <XAxis type="number" tick={{ fontSize: 10, fill: "#9ca3af" }} axisLine={false} tickLine={false}
                   tickFormatter={v => `${v > 0 ? "+" : ""}${v}%`} />
            <YAxis type="category" dataKey="category_name" width={115}
                   tick={{ fontSize: 11, fill: "#6b7280" }} axisLine={false} tickLine={false} />
            <Tooltip
              formatter={(v: number) => [`${v > 0 ? "+" : ""}${v.toFixed(1)}%`, "Change"]}
              contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }}
            />
            <Bar dataKey="pct_change" radius={[0, 4, 4, 0]}>
              {sorted.map((e, i) => <Cell key={i} fill={e.pct_change >= 0 ? "#4a0f70" : "#ef4444"} />)}
              <LabelList dataKey="pct_change"
                         formatter={(v: number) => `${v > 0 ? "+" : ""}${v.toFixed(1)}%`}
                         style={{ fill: "#6b7280", fontSize: 10 }}
                         position="right" />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Category table */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100">
          <p className="text-sm font-semibold text-gray-800">Changes in Category Allocation</p>
          <p className="text-xs text-gray-600 mt-0.5">Identify top changes in activity application/site allocation to understand technology usage trends.</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[640px]">
          <thead>
            <tr className="bg-gray-50 text-gray-600 text-xs uppercase tracking-wide">
              <th className="text-left px-5 py-2.5 font-semibold">Category</th>
              <th className="text-left px-5 py-2.5 font-semibold">Allocation % Chg</th>
              <th className="text-left px-5 py-2.5 font-semibold">Allocation % (Activity Date)</th>
              <th className="text-right px-5 py-2.5 font-semibold">Hrs</th>
              <th className="text-right px-5 py-2.5 font-semibold">Users Chg</th>
            </tr>
          </thead>
          <tbody>
            {[...allocation].sort((a, b) => Math.abs(b.pct_change) - Math.abs(a.pct_change)).map(item => (
              <tr key={item.category_name} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-5 py-3 flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: item.color }} />
                  <span className="font-medium text-gray-800">{item.category_name}</span>
                </td>
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <PctBadge v={item.pct_change} />
                    <div className="w-20 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                      <div className="h-full rounded-full"
                           style={{ width: `${Math.min(Math.abs(item.pct_change) * 8, 100)}%`,
                                    background: item.pct_change >= 0 ? "#4a0f70" : "#ef4444" }} />
                    </div>
                  </div>
                </td>
                <td className="px-5 py-3 text-gray-600">{item.pct.toFixed(1)}%</td>
                <td className="px-5 py-3 text-right text-gray-600">{item.hours.toFixed(1)}</td>
                <td className="px-5 py-3 text-right"><PctBadge v={item.user_change} /></td>
              </tr>
            ))}
            {allocation.length === 0 && (
              <tr><td colSpan={5} className="px-5 py-10 text-center text-gray-600">No data yet.</td></tr>
            )}
          </tbody>
        </table>
        </div>
      </div>

      {/* Trending up / down */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <TrendTable title="Applications/Sites Trending Up"   icon={TrendingUp}   items={trends.trending_up}   up />
        <TrendTable title="Applications/Sites Trending Down" icon={TrendingDown} items={trends.trending_down} up={false} />
      </div>
    </div>
  );
}

function TrendTable({ title, icon: Icon, items, up }: {
  title: string; icon: React.ElementType; items: AppTrends["trending_up"]; up: boolean;
}) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-2">
        <Icon size={14} className={up ? "text-green-500" : "text-red-400"} />
        <p className="text-sm font-semibold text-gray-800">{title}</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm min-w-[640px]">
        <thead>
          <tr className="bg-gray-50 text-gray-600 text-xs uppercase tracking-wide">
            <th className="text-left px-4 py-2 font-semibold">App / Site</th>
            <th className="text-left px-4 py-2 font-semibold">Alloc % Chg</th>
            <th className="text-right px-4 py-2 font-semibold">Alloc %</th>
            <th className="text-right px-4 py-2 font-semibold">Hrs</th>
            <th className="text-right px-4 py-2 font-semibold">Users</th>
          </tr>
        </thead>
        <tbody>
          {items.map(app => (
            <tr key={app.app_name} className="border-t border-gray-100 hover:bg-gray-50">
              <td className="px-4 py-2.5">
                <p className="font-medium text-gray-800 text-xs truncate max-w-[110px]">{app.app_name}</p>
                <p className="text-[11px] text-gray-600">{app.category_name}</p>
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center gap-1.5">
                  <PctBadge v={app.pct_change} />
                  <div className="w-12 h-1 bg-gray-100 rounded-full overflow-hidden">
                    <div className="h-full rounded-full"
                         style={{ width: `${Math.min(Math.abs(app.pct_change) * 15, 100)}%`, background: app.color }} />
                  </div>
                </div>
              </td>
              <td className="px-4 py-2.5 text-right text-xs text-gray-600">{app.pct.toFixed(1)}%</td>
              <td className="px-4 py-2.5 text-right text-xs text-gray-600">{app.hours.toFixed(1)}</td>
              <td className="px-4 py-2.5 text-right"><PctBadge v={app.user_change} /></td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-600">No changes in this period.</td></tr>
          )}
        </tbody>
      </table>
      </div>
    </div>
  );
}

// ── Adoption tab ──────────────────────────────────────────────────────────────

function AdoptionTab({ data }: { data: AdoptionData }) {
  if (!data.apps.length) return (
    <div className="bg-white border border-gray-200 rounded-xl p-10 shadow-sm text-center">
      <p className="text-sm text-gray-600">No adoption data for this period.</p>
    </div>
  );
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
      <p className="text-sm font-semibold text-gray-800 mb-0.5">Application Adoption Over Time</p>
      <p className="text-xs text-gray-600 mb-4">Daily unique users per top application</p>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={data.series} margin={{ top: 4, right: 16, bottom: 4, left: 0 }}>
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#9ca3af" }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fontSize: 11, fill: "#9ca3af" }} axisLine={false} tickLine={false} width={28} />
          <Tooltip contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }} />
          <Legend wrapperStyle={{ fontSize: 12, paddingTop: 12 }} />
          {data.apps.map((app, i) => (
            <Line key={app} type="monotone" dataKey={app}
                  stroke={LINE_COLORS[i % LINE_COLORS.length]} strokeWidth={2}
                  dot={false} activeDot={{ r: 4 }} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

type Tab = "overview" | "top-changes" | "adoption";

export default function TechUtilizationPage() {
  const { loading, can } = useUser();
  const [tab,        setTab]        = useState<Tab>("overview");
  // Default to TODAY only — a single-day aggregation is far cheaper than 30 days,
  // which is the main reason this page's concurrent query burst was saturating
  // the backend. Users widen the range via the filter when they need history.
  const [dateRange,  setDateRange]  = useState<DateRange>({ since: format(new Date(), "yyyy-MM-dd"), until: format(new Date(), "yyyy-MM-dd") });
  const [groupId,    setGroupId]    = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [categoryId, setCategoryId] = useState("");

  const { data: users }      = useSWR("tech-users-tf", () => import("@/lib/api").then(m => m.getTechFilterUsers()));
  const { data: groups }     = useSWR("tech-groups-tf", () => import("@/lib/api").then(m => m.getTechFilterGroups()));
  const { data: catsPage } = useSWR("tech-cats-filter", () => getTechCategories({ per_page: 200 }));
  const categories: AppCategory[] = catsPage?.items ?? [];

  // summary feeds the always-visible stat cards, so it always loads.
  const { data: summary, isLoading: summaryLoading } = useSWR(
    ["tech-sum",   dateRange.since, dateRange.until, groupId, employeeId],
    () => getTechSummary(dateRange.since, dateRange.until, groupId || undefined, employeeId || undefined),
  );
  // Everything below is fetched ONLY for the tab that needs it (key = null when
  // inactive → SWR skips the request). This turns the old 7-request mount burst
  // — which was saturating the single backend worker and producing the CORS/5xx
  // errors — into ~4 concurrent requests, and tab data loads on demand.
  const { data: allocation } = useSWR(
    tab !== "adoption" ? ["tech-alloc", dateRange.since, dateRange.until, groupId, employeeId] : null,
    () => getCategoryAllocation(dateRange.since, dateRange.until, groupId || undefined, employeeId || undefined),
  );
  const { data: trends } = useSWR(
    tab === "top-changes" ? ["tech-trends", dateRange.since, dateRange.until, groupId, employeeId] : null,
    () => getAppTrends(dateRange.since, dateRange.until, 10),
  );
  const { data: apps } = useSWR(
    tab === "overview" ? ["tech-apps", dateRange.since, dateRange.until, categoryId, groupId, employeeId] : null,
    () => getAppList(dateRange.since, dateRange.until, categoryId || undefined, 50, groupId || undefined, employeeId || undefined),
  );
  const { data: adoption } = useSWR(
    tab === "adoption" ? ["tech-adoption", dateRange.since, dateRange.until] : null,
    () => getAdoptionData(dateRange.since, dateRange.until, 5),
  );


  if (loading) return <PageLoader />;
  if (!can("tech", "view")) return <AccessDenied label="Technology Utilization" />;
  if (summaryLoading && !summary) return <PageSkeleton variant="dashboard" />;

  const TABS: { key: Tab; label: string }[] = [
    { key: "overview",    label: "Overview"    },
    { key: "top-changes", label: "Top Changes" },
    { key: "adoption",    label: "Adoption"    },
  ];

  const statCards = [
    { label: "Total Hours",    value: (summary?.total_hours ?? 0).toLocaleString() + "h", icon: Clock,   color: "bg-brand-50 text-brand-600" },
    { label: "Applications",   value: summary?.total_apps   ?? 0,                          icon: Monitor, color: "bg-purple-50 text-purple-600" },
    { label: "Users Tracked",  value: summary?.total_users  ?? 0,                          icon: Users,   color: "bg-green-50 text-green-600" },
    { label: summary?.top_category ?? "Top Category",
      value: summary?.top_app ?? "—",
      icon: Cpu,
      color: "bg-gray-50 text-gray-600",
      sub: summary?.top_category },
  ];

  return (
    <div className="overflow-y-auto pr-1">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Technology Utilization</h1>
          <p className="text-sm text-gray-600 mt-0.5">Track application usage and category allocation across your organisation</p>
        </div>
      </div>

      {/* Filter chips */}
      <div className="flex flex-wrap items-center gap-2 mb-5">
        {/* Activity Date */}
        <DateRangePicker value={dateRange} onChange={setDateRange} />
        {/* Team / Group */}
        <GroupPicker value={groupId} onChange={v => { setGroupId(v); setEmployeeId(""); }} groups={groups} />

        {/* User */}
        <div className="relative">
          <select value={employeeId} onChange={e => { setEmployeeId(e.target.value); setGroupId(""); }}
                  className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-7 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300">
            <option value="">All Users</option>
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
            {((users as any[]) ?? []).filter((u: any) => u.is_active).map((u: any) => (
              <option key={u.id} value={u.id}>{u.name}</option>
            ))}
          </select>
          <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
        </div>

        {/* Category filter */}
        <div className="relative">
          <select value={categoryId} onChange={e => setCategoryId(e.target.value)}
                  className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-7 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300">
            <option value="">All Categories</option>
            {(categories ?? []).map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
        </div>

        {(groupId || employeeId || categoryId) && (
          <button onClick={() => { setGroupId(""); setEmployeeId(""); setCategoryId(""); }}
                  className="text-xs text-gray-600 hover:text-gray-700 underline">
            Clear filters
          </button>
        )}
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-5">
        {statCards.map((s, i) => (
          <div key={i} className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
            <div className="flex items-center gap-2 mb-2">
              <div className={`w-8 h-8 rounded-lg ${s.color.split(" ")[0]} flex items-center justify-center`}>
                <s.icon size={15} className={s.color.split(" ")[1]} />
              </div>
              <p className="text-xs text-gray-600 font-medium uppercase tracking-wide truncate">{s.label}</p>
            </div>
            <p className="text-2xl font-bold text-gray-900 truncate">{s.value}</p>
            {s.sub && <p className="text-xs text-gray-600 mt-0.5 truncate">Category: {s.sub}</p>}
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 mb-5">
        <div className="flex gap-0">
          {TABS.map(t => (
            <button key={t.key} onClick={() => setTab(t.key)}
                    className={`px-5 py-2.5 text-sm font-medium border-b-2 transition -mb-px ${
                      tab === t.key
                        ? "border-brand-600 text-brand-600"
                        : "border-transparent text-gray-600 hover:text-gray-800"
                    }`}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content — each tab shows a skeleton until its (lazy) data arrives.
          Loaded-but-empty responses are arrays/objects (truthy), so the skeleton
          only shows while genuinely fetching, never on a real empty result. */}
      {tab === "overview" && (
        allocation && apps
          ? <OverviewTab allocation={allocation} apps={apps} categoryId={categoryId} />
          : <TabSkeleton />
      )}
      {tab === "top-changes" && (
        allocation && trends
          ? <TopChangesTab allocation={allocation} trends={trends} />
          : <TabSkeleton />
      )}
      {tab === "adoption" && (
        adoption
          ? <AdoptionTab data={adoption} />
          : <TabSkeleton />
      )}
    </div>
  );
}
