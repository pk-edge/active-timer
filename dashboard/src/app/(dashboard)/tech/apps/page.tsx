"use client";
import PageLoader from "@/components/PageLoader";
import { format } from "date-fns";
import { useState, useMemo, useEffect } from "react";
import useSWR from "swr";
import dynamic from "next/dynamic";
import { Monitor, Download, MoreHorizontal, ChevronLeft } from "lucide-react";
import { getAppUsage, getAppUsageDetail } from "@/lib/api";

// Lazy: the per-app usage chart loads recharts only when the "Usage" tab opens.
const UsageBarChart = dynamic(() => import("./_components/UsageBarChart"), {
  ssr: false,
  loading: () => <div className="h-[220px] animate-pulse bg-gray-50 rounded-lg" />,
});
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import {
  SmartSearchBar, FilterBar, QuickStats, DetailTabs,
  DurationRow, ProdIcon, fmtDur, exportCsv, Pager, LIST_PAGE_SIZE,
  type DateRange } from "@/components/UsagePageShell";
import type { AppUsageItem, AppUsageDetailOut } from "@/types";

const DETAIL_TABS = ["Titles", "Users", "Usage"];

// ── Right panel ───────────────────────────────────────────────────────────────

function DetailPanel({
  item, since, until, groupId, employeeId }: { item: AppUsageItem; since: string; until: string; groupId: string; employeeId: string }) {
  const [tab, setTab] = useState("Titles");

  const { data, isLoading } = useSWR<AppUsageDetailOut>(
    ["app-detail", item.app_name, tab.toLowerCase(), since, until, groupId, employeeId],
    () => getAppUsageDetail(item.app_name, tab.toLowerCase(), since, until, groupId || undefined, employeeId || undefined),
  );

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-5 py-4 border-b border-gray-200 shrink-0">
        <div className="flex items-center gap-2 mb-1">
          <div className="w-6 h-6 rounded-lg bg-gray-100 flex items-center justify-center shrink-0"
               style={{ background: `${item.color}20` }}>
            <Monitor size={13} style={{ color: item.color }} />
          </div>
          <p className="font-semibold text-gray-900 text-sm truncate flex-1">{item.app_name}</p>
        </div>
        <p className="text-xs text-gray-600">
          {fmtDur(item.duration_secs)} · {item.user_count} {item.user_count === 1 ? "user" : "users"} · {item.pct.toFixed(1)}%
        </p>
        {item.category_name && (
          <span className="inline-block text-xs px-2 py-0.5 rounded-full mt-1"
                style={{ background: `${item.color}15`, color: item.color }}>
            {item.category_name}
          </span>
        )}
      </div>

      {/* Tabs */}
      <div className="px-5 shrink-0">
        <DetailTabs tabs={DETAIL_TABS} active={tab} onSelect={setTab} />
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto px-5 min-h-0">
        {isLoading ? (
          <p className="text-sm text-gray-600 py-8 text-center">Loading…</p>
        ) : tab === "Titles" ? (
          <>
            {(data?.titles ?? []).length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No window titles recorded.</p>
              : (data?.titles ?? []).map(t => (
                <DurationRow key={t.title} label={t.title}
                             duration_secs={t.duration_secs} pct={t.pct} color={item.color} />
              ))}
          </>
        ) : tab === "Users" ? (
          <>
            {(data?.users ?? []).length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No users.</p>
              : (data?.users ?? []).map(u => (
                <DurationRow key={u.name} label={u.name}
                             duration_secs={u.duration_secs} pct={u.pct} color={item.color} />
              ))}
          </>
        ) : (
          <div className="pt-2">
            {(data?.usage ?? []).length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No data.</p>
              : <UsageBarChart data={data?.usage ?? []} color={item.color} />}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function AppUsagePage() {
  const { loading, can } = useUser();
  const [search,       setSearch]       = useState("");
  // Default to TODAY only (cheap single-day query; widen via the filter for history).
  const [dateRange, setDateRange] = useState<DateRange>({ since: format(new Date(), 'yyyy-MM-dd'), until: format(new Date(), 'yyyy-MM-dd') });
  const [groupId,      setGroupId]      = useState("");
  const [employeeId,   setEmployeeId]   = useState("");
  const [productivity, setProductivity] = useState("");
  const [summaryMode,  setSummaryMode]  = useState(true);
  const [selected,     setSelected]     = useState<AppUsageItem | null>(null);

  const { data, isLoading } = useSWR(
    ["app-usage", dateRange.since, dateRange.until, productivity, groupId, employeeId],
    () => getAppUsage(dateRange.since, dateRange.until, productivity || undefined, groupId || undefined, employeeId || undefined),
  );

  const filtered = useMemo(() => {
    const items = data?.items ?? [];
    if (!search.trim()) return items;
    const q = search.toLowerCase();
    return items.filter(i =>
      i.app_name.toLowerCase().includes(q) || (i.category_name ?? "").toLowerCase().includes(q)
    );
  }, [data, search]);

  // Client-side pagination over the (today-scoped) list — keeps the DOM light.
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [search, dateRange.since, dateRange.until, groupId, employeeId, productivity]);
  const paged = useMemo(
    () => filtered.slice((page - 1) * LIST_PAGE_SIZE, page * LIST_PAGE_SIZE),
    [filtered, page],
  );

  function doExport() {
    exportCsv(
      ["Application", "Duration", "Users", "Allocation %", "Productivity", "Category"],
      filtered.map(i => [i.app_name, fmtDur(i.duration_secs), String(i.user_count),
                         `${i.pct}%`, i.productivity, i.category_name ?? ""]),
      "app-usage.csv",
    );
  }

  if (loading) return <PageLoader />;
  if (!can("tech", "view")) return <AccessDenied label="Application Usage" />;

  const stats = [
    { label: "Total Duration",        value: fmtDur(data?.total_secs ?? 0),        color: "text-brand-600" },
    { label: "Total Applications",    value: String(data?.total_apps ?? 0) },
    { label: "Productive Apps",       value: String(data?.items.filter(i => i.productivity === "productive").length ?? 0), color: "text-green-600" },
    { label: "Unproductive Apps",     value: String(data?.items.filter(i => i.productivity === "unproductive").length ?? 0), color: "text-red-500" },
  ];

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-4 shrink-0">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Application Usage</h1>
          <p className="text-sm text-gray-600 mt-0.5">Desktop app activity from all devices</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={doExport}
                  className="flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm transition">
            <Download size={13} /> Export
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="flex gap-2 mb-4 shrink-0">
        <SmartSearchBar value={search} onChange={setSearch} placeholder="Search by application or category…" />
      </div>

      {/* Filters */}
      <div className="shrink-0">
        <FilterBar
          dateRange={dateRange} setDateRange={setDateRange}
          groupId={groupId} setGroupId={setGroupId}
          employeeId={employeeId} setEmployeeId={setEmployeeId}
          productivity={productivity} setProductivity={setProductivity}
          summaryMode={summaryMode} setSummaryMode={setSummaryMode}
        />
      </div>

      {/* Quick stats */}
      <div className="shrink-0">
        <QuickStats stats={stats} />
      </div>

      {/* Two-panel layout */}
      <div className="flex flex-1 min-h-0 bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        {/* Left — app list. Hidden on mobile when an item is selected. */}
        <div className={`flex-col min-h-0 lg:border-r border-gray-200 ${
          selected ? "hidden lg:flex lg:w-[55%]" : "flex w-full"
        }`}>
          <div className="grid grid-cols-[28px_1fr_90px_52px_28px] sm:grid-cols-[32px_1fr_120px_60px_32px] gap-2 px-3 sm:px-4 py-2.5 bg-gray-50 border-b border-gray-200 shrink-0">
            <div />
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">Application</p>
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">Duration</p>
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide text-right">%</p>
            <div />
          </div>

          <div className="flex-1 overflow-y-auto min-h-0">
            {isLoading ? (
              <p className="text-sm text-gray-600 py-12 text-center animate-pulse">Loading…</p>
            ) : filtered.length === 0 ? (
              <p className="text-sm text-gray-600 py-12 text-center">No applications found.</p>
            ) : paged.map(item => (
              <button
                key={item.app_name}
                onClick={() => setSelected(prev => prev?.app_name === item.app_name ? null : item)}
                className={`w-full grid grid-cols-[28px_1fr_90px_52px_28px] sm:grid-cols-[32px_1fr_120px_60px_32px] gap-2 px-3 sm:px-4 py-3 border-b border-gray-100 hover:bg-brand-50/40 transition text-left ${
                  selected?.app_name === item.app_name ? "bg-brand-50" : ""
                }`}
              >
                <ProdIcon v={item.productivity} />
                <div className="min-w-0">
                  <p className="text-sm font-medium text-gray-800 truncate">{item.app_name}</p>
                  {item.category_name && (
                    <p className="text-xs truncate" style={{ color: item.color }}>{item.category_name}</p>
                  )}
                </div>
                <p className="text-sm text-gray-700 tabular-nums self-center">{fmtDur(item.duration_secs)}</p>
                <p className="text-sm text-gray-600 tabular-nums text-right self-center">{item.pct.toFixed(1)}%</p>
                <MoreHorizontal size={14} className="text-gray-500 self-center" />
              </button>
            ))}
          </div>
          <Pager page={page} total={filtered.length} onPage={setPage} />
        </div>

        {/* Right — detail panel. Full-width on mobile, side panel on desktop. */}
        {selected && (
          <div className="flex flex-col min-h-0 w-full lg:flex-1 lg:min-w-0">
            <button
              onClick={() => setSelected(null)}
              className="lg:hidden flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium text-brand-600 border-b border-gray-100 shrink-0"
            >
              <ChevronLeft size={16} /> Back to list
            </button>
            <DetailPanel item={selected} since={dateRange.since} until={dateRange.until} groupId={groupId} employeeId={employeeId} />
          </div>
        )}
      </div>

      {!selected && filtered.length > 0 && (
        <p className="text-xs text-gray-600 text-center mt-2 shrink-0">
          Tap an application to see details
        </p>
      )}
    </div>
  );
}
