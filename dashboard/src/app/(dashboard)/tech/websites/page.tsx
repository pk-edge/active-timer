"use client";
import PageLoader from "@/components/PageLoader";
import { format } from "date-fns";
import { useState, useMemo, useEffect } from "react";
import useSWR from "swr";
import dynamic from "next/dynamic";
import Link from "next/link";
import { Globe, Download, MoreHorizontal, ExternalLink, ChevronLeft, ArrowLeft } from "lucide-react";
import { getWebsites, getWebsiteDetail } from "@/lib/api";

// Lazy: the per-site visits chart loads recharts only when the "Usage" tab opens.
const UsageLineChart = dynamic(() => import("./_components/UsageLineChart"), {
  ssr: false,
  loading: () => <div className="h-[220px] animate-pulse bg-gray-50 rounded-lg" />,
});
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { AccessDenied } from "@/components/AccessDenied";
import {
  SmartSearchBar, FilterBar, QuickStats, DetailTabs,
  DurationRow, ProdIcon, fmtDur, exportCsv, Pager, LIST_PAGE_SIZE,
  ListSkeletonRows, DetailSkeletonRows,
  type DateRange } from "@/components/UsagePageShell";
import { smartSort } from "@/lib/smartSearch";
import type { WebsiteItem, WebsiteDetailOut } from "@/types";

const DETAIL_TABS = ["Titles", "Subpages", "Users", "Usage"];

// ── Right panel ───────────────────────────────────────────────────────────────

function DetailPanel({
  item, since, until, groupId, employeeId }: { item: WebsiteItem; since: string; until: string; groupId: string; employeeId: string }) {
  const [tab, setTab] = useState("Titles");

  const { data, isLoading, isValidating } = useSWR<WebsiteDetailOut>(
    ["website-detail", item.host, tab.toLowerCase(), since, until, groupId, employeeId],
    () => getWebsiteDetail(item.host, tab.toLowerCase(), since, until, groupId || undefined, employeeId || undefined),
  );
  // Cold load only — keepPreviousData means isLoading goes false the instant
  // any data exists, even for a different tab/filter key. isValidating covers
  // the background-refetch case (switching tabs, changing filters) instead.
  const isRefreshing = isValidating && !!data;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-5 py-4 border-b border-gray-200 shrink-0">
        <div className="flex items-center gap-2 mb-1">
          <div className="w-5 h-5 rounded bg-gray-100 flex items-center justify-center shrink-0">
            <Globe size={12} className="text-gray-600" />
          </div>
          <p className="font-semibold text-gray-900 text-sm truncate">{item.host}</p>
          {isRefreshing && (
            <span className="text-xs text-gray-400 animate-pulse shrink-0">Updating…</span>
          )}
          <a href={`https://${item.host}`} target="_blank" rel="noreferrer"
             className="text-gray-600 hover:text-brand-500 ml-auto shrink-0">
            <ExternalLink size={13} />
          </a>
        </div>
        <p className="text-xs text-gray-600">
          {fmtDur(item.duration_secs)} · {item.visits.toLocaleString()} visits · {item.pct.toFixed(1)}%
        </p>
      </div>

      {/* Tabs */}
      <div className="px-5 shrink-0">
        <DetailTabs tabs={DETAIL_TABS} active={tab} onSelect={setTab} />
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto px-5 min-h-0">
        {isLoading ? (
          <DetailSkeletonRows />
        ) : tab === "Titles" ? (
          <>
            {(data?.titles ?? []).length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No data.</p>
              : (data?.titles ?? []).map(t => (
                <DurationRow key={t.title} label={t.title}
                             duration_secs={t.visits * 60} pct={t.pct} color={item.color} />
              ))}
          </>
        ) : tab === "Subpages" ? (
          <>
            {(data?.subpages ?? []).length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No data.</p>
              : (data?.subpages ?? []).map(s => (
                <div key={s.path} className="py-2 border-b border-gray-100 last:border-0">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs text-gray-700 truncate font-mono flex-1" title={s.path}>{s.path}</p>
                    <div className="flex items-center gap-3 shrink-0">
                      <span className="text-xs text-gray-600 tabular-nums">{s.visits} visits</span>
                      <span className="text-xs text-gray-600">{s.pct.toFixed(1)}%</span>
                    </div>
                  </div>
                </div>
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
          /* Usage chart */
          <div className="pt-2">
            {(data?.usage ?? []).length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No data.</p>
              : <UsageLineChart data={data?.usage ?? []} color={item.color} />}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function WebsiteUsagePage() {
  const { loading, can } = useUser();
  const [search,       setSearch]       = useState("");
  // Default to TODAY only (cheap single-day query; widen via the filter for history).
  const [dateRange, setDateRange] = useState<DateRange>({ since: format(new Date(), 'yyyy-MM-dd'), until: format(new Date(), 'yyyy-MM-dd') });
  const [groupId,      setGroupId]      = useState("");
  const [employeeId,   setEmployeeId]   = useState("");
  const [productivity, setProductivity] = useState("");
  const [summaryMode,  setSummaryMode]  = useState(true);
  const [selected,     setSelected]     = useState<WebsiteItem | null>(null);

  const { data, isLoading, isValidating } = useSWR(
    ["websites", dateRange.since, dateRange.until, productivity, groupId, employeeId],
    () => getWebsites(dateRange.since, dateRange.until, productivity || undefined, groupId || undefined, employeeId || undefined),
  );
  // isValidating is true during background revalidation (stale data is still
  // shown, per the global keepPreviousData config) — a filter change re-keys
  // this call, but without this the old list just sits there with no
  // indication a new fetch is running until it silently swaps. Same
  // "Updating…" convention as Organization Overview.
  const isRefreshing = isValidating && !!data;

  const debouncedSearch = useDebouncedValue(search, 200);

  const filtered = useMemo(() => {
    const items = data?.items ?? [];
    return smartSort(debouncedSearch, items, i => [i.host, i.category_name]);
  }, [data, debouncedSearch]);

  // Client-side pagination over the (today-scoped) list — keeps the DOM light.
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [debouncedSearch, dateRange.since, dateRange.until, groupId, employeeId, productivity]);
  const paged = useMemo(
    () => filtered.slice((page - 1) * LIST_PAGE_SIZE, page * LIST_PAGE_SIZE),
    [filtered, page],
  );

  function doExport() {
    exportCsv(
      ["Website", "Duration", "Visits", "Users", "Allocation %", "Productivity", "Category"],
      filtered.map(i => [i.host, fmtDur(i.duration_secs), String(i.visits),
                         String(i.user_count), `${i.pct}%`, i.productivity, i.category_name ?? ""]),
      "website-usage.csv",
    );
  }

  if (loading) return <PageLoader />;
  if (!can("tech", "view")) return <AccessDenied label="Website Usage" />;

  const stats = [
    { label: "Total Duration",    value: fmtDur(data?.total_secs ?? 0),         color: "text-brand-600" },
    { label: "Total Visits",      value: (data?.total_visits ?? 0).toLocaleString() },
    { label: "Unique Websites",   value: String(data?.items.length ?? 0) },
    { label: "Productive Sites",  value: String(data?.items.filter(i => i.productivity === "productive").length ?? 0), color: "text-green-600" },
  ];

  return (
    <div className="flex flex-col h-full min-h-0">
      <Link href="/tech" className="inline-flex items-center gap-1.5 text-gray-600 text-sm mb-3 hover:text-gray-700 transition w-fit">
        <ArrowLeft size={13} /> Tech Utilization
      </Link>
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-4 shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Website Usage</h1>
            {isRefreshing && (
              <span className="text-xs text-gray-400 animate-pulse">Updating…</span>
            )}
          </div>
          <p className="text-sm text-gray-600 mt-0.5">Browser activity from all devices</p>
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
        <SmartSearchBar value={search} onChange={setSearch} placeholder="Search by website or category…" />
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
      <div className="flex gap-0 flex-1 min-h-0 bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        {/* Left — website list. Hidden on mobile when an item is selected. */}
        <div className={`flex-col min-h-0 lg:border-r border-gray-200 ${
          selected ? "hidden lg:flex lg:w-[55%]" : "flex w-full"
        }`}>
          {/* Table header */}
          <div className="grid grid-cols-[28px_1fr_90px_52px_28px] sm:grid-cols-[32px_1fr_110px_60px_32px] gap-2 px-3 sm:px-4 py-2.5 bg-gray-50 border-b border-gray-200 shrink-0">
            <div />
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">Website</p>
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">Duration</p>
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide text-right">%</p>
            <div />
          </div>

          {/* Rows */}
          <div className="flex-1 overflow-y-auto min-h-0" aria-busy={isLoading || undefined}>
            {isLoading ? (
              <ListSkeletonRows
                gridClassName="grid-cols-[28px_1fr_90px_52px_28px] sm:grid-cols-[32px_1fr_110px_60px_32px]"
                rowClassName="gap-2 px-3 sm:px-4 py-3"
                columns={["icon", "text", "duration", "pct", "menu"]}
              />
            ) : filtered.length === 0 ? (
              <p className="text-sm text-gray-600 py-12 text-center">No websites found.</p>
            ) : paged.map(item => (
              <button
                key={item.host}
                onClick={() => setSelected(prev => prev?.host === item.host ? null : item)}
                className={`w-full grid grid-cols-[28px_1fr_90px_52px_28px] sm:grid-cols-[32px_1fr_110px_60px_32px] gap-2 px-3 sm:px-4 py-3 border-b border-gray-100 hover:bg-brand-50/40 transition text-left ${
                  selected?.host === item.host ? "bg-brand-50" : ""
                }`}
              >
                <ProdIcon v={item.productivity} />
                <div className="min-w-0">
                  <p className="text-sm font-medium text-gray-800 truncate">{item.host}</p>
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
            {/* Mobile back bar */}
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
          Tap a website to see details
        </p>
      )}
    </div>
  );
}
