"use client";
import PageLoader from "@/components/PageLoader";
import { format } from "date-fns";
import { useState, useMemo, useEffect } from "react";
import useSWR from "swr";
import Link from "next/link";
import { ArrowLeft, ArrowUpRight, ChevronLeft } from "lucide-react";
import { getCategoryUsage, getCategoryUsageDetail } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { AccessDenied } from "@/components/AccessDenied";
import {
  SmartSearchBar, FilterBar, QuickStats, DetailTabs,
  DurationRow, fmtDur, exportCsv, Pager, LIST_PAGE_SIZE,
  type DateRange } from "@/components/UsagePageShell";
import { smartSort } from "@/lib/smartSearch";
import type { CategoryUsageItem, CategoryUsageDetailOut } from "@/types";

const DETAIL_TABS = ["Activities", "Users"];

// ── Right panel ───────────────────────────────────────────────────────────────

function DetailPanel({
  item, since, until, groupId }: { item: CategoryUsageItem; since: string; until: string; groupId: string }) {
  const [tab, setTab] = useState("Activities");
  const [userSearch, setUserSearch] = useState("");

  const { data, isLoading } = useSWR<CategoryUsageDetailOut>(
    ["cat-detail", item.cat_id, tab.toLowerCase(), since, until, groupId],
    () => getCategoryUsageDetail(item.cat_id, tab.toLowerCase(), since, until, groupId || undefined),
  );

  const debouncedUserSearch = useDebouncedValue(userSearch, 200);

  const displayUsers = useMemo(() => {
    const list = data?.users ?? [];
    return smartSort(debouncedUserSearch, list, u => [u.name, u.email]);
  }, [data?.users, debouncedUserSearch]);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-5 py-4 border-b border-gray-200 shrink-0">
        <div className="flex items-center gap-2 mb-1">
          <div className="w-3 h-3 rounded-full shrink-0" style={{ background: item.color }} />
          <p className="font-semibold text-gray-900 text-sm flex-1">{item.name}</p>
          <p className="text-xs text-gray-600 shrink-0">
            Total Duration: <span className="font-medium text-gray-700">{fmtDur(item.duration_secs)}</span>
          </p>
        </div>
        <p className="text-xs text-gray-600">
          {item.pct.toFixed(1)}% of total · {item.user_count} {item.user_count === 1 ? "user" : "users"}
        </p>
      </div>

      {/* Tabs + action */}
      <div className="px-5 flex items-center justify-between shrink-0">
        <DetailTabs tabs={DETAIL_TABS} active={tab} onSelect={setTab} />
        {item.cat_id && (
          <Link href="/categories"
                className="flex items-center gap-1 text-xs text-brand-600 hover:text-brand-700 font-medium shrink-0 pb-2">
            Manage Rules <ArrowUpRight size={11} />
          </Link>
        )}
      </div>

      {/* Search for Users tab */}
      {tab === "Users" && (
        <div className="px-5 pb-3 shrink-0">
          <SmartSearchBar value={userSearch} onChange={setUserSearch} placeholder="Search users…" />
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-5 min-h-0">
        {isLoading ? (
          <p className="text-sm text-gray-600 py-8 text-center">Loading…</p>
        ) : tab === "Activities" ? (
          <>
            {(data?.activities ?? []).length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No applications in this category.</p>
              : (data?.activities ?? []).map(a => (
                <DurationRow key={a.app_name} label={a.app_name}
                             duration_secs={a.duration_secs} pct={a.pct} color={item.color} />
              ))}
          </>
        ) : (
          <>
            {displayUsers.length === 0
              ? <p className="text-sm text-gray-600 py-8 text-center">No users.</p>
              : displayUsers.map(u => (
                <DurationRow key={u.email} label={u.name} sub={u.email}
                             duration_secs={u.duration_secs} pct={u.pct} color={item.color} />
              ))}
          </>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function CategoryUsagePage() {
  const { loading, can } = useUser();
  const [search,      setSearch]      = useState("");
  // Default to TODAY only (cheap single-day query; widen via the filter for history).
  const [dateRange, setDateRange] = useState<DateRange>({ since: format(new Date(), 'yyyy-MM-dd'), until: format(new Date(), 'yyyy-MM-dd') });
  const [groupId,     setGroupId]     = useState("");
  const [employeeId,  setEmployeeId]  = useState("");
  const [summaryMode, setSummaryMode] = useState(true);
  const [selected,    setSelected]    = useState<CategoryUsageItem | null>(null);

  const { data, isLoading, isValidating } = useSWR(
    ["category-usage", dateRange.since, dateRange.until, groupId, employeeId],
    () => getCategoryUsage(dateRange.since, dateRange.until, groupId || undefined),
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
    return smartSort(debouncedSearch, items, i => [i.name]);
  }, [data, debouncedSearch]);

  // Client-side pagination over the (today-scoped) list — keeps the DOM light.
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [debouncedSearch, dateRange.since, dateRange.until, groupId, employeeId]);
  const paged = useMemo(
    () => filtered.slice((page - 1) * LIST_PAGE_SIZE, page * LIST_PAGE_SIZE),
    [filtered, page],
  );

  function doExport() {
    exportCsv(
      ["Category", "Duration", "Users", "Allocation %"],
      filtered.map(i => [i.name, fmtDur(i.duration_secs), String(i.user_count), `${i.pct}%`]),
      "category-usage.csv",
    );
  }

  if (loading) return <PageLoader />;
  if (!can("tech", "view")) return <AccessDenied label="Category Usage" />;

  const stats = [
    { label: "Total Duration",   value: fmtDur(data?.total_secs ?? 0),         color: "text-brand-600" },
    { label: "Categories",       value: String(data?.items.length ?? 0) },
    { label: "Uncategorized",    value: fmtDur(data?.items.find(i => i.name === "Uncategorized")?.duration_secs ?? 0), color: "text-amber-500" },
    { label: "Top Category",     value: data?.items[0]?.name ?? "—" },
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
            <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Category Usage</h1>
            {isRefreshing && (
              <span className="text-xs text-gray-400 animate-pulse">Updating…</span>
            )}
          </div>
          <p className="text-sm text-gray-600 mt-0.5">Time broken down by application category</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={doExport}
                  className="flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm transition">
            Export
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="flex gap-2 mb-4 shrink-0">
        <SmartSearchBar value={search} onChange={setSearch} placeholder="Search by category…" />
      </div>

      {/* Filters — no productivity filter for category view */}
      <div className="shrink-0">
        <FilterBar
          dateRange={dateRange} setDateRange={setDateRange}
          groupId={groupId} setGroupId={setGroupId}
          employeeId={employeeId} setEmployeeId={setEmployeeId}
          showProdFilter={false}
          summaryMode={summaryMode} setSummaryMode={setSummaryMode}
        />
      </div>

      {/* Quick stats */}
      <div className="shrink-0">
        <QuickStats stats={stats} />
      </div>

      {/* Two-panel */}
      <div className="flex flex-1 min-h-0 bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        {/* Left — category list. Hidden on mobile when an item is selected. */}
        <div className={`flex-col min-h-0 lg:border-r border-gray-200 ${
          selected ? "hidden lg:flex lg:w-[45%]" : "flex w-full"
        }`}>
          <div className="grid grid-cols-[16px_1fr_110px_52px] sm:grid-cols-[16px_1fr_120px_60px] gap-3 px-4 sm:px-5 py-2.5 bg-gray-50 border-b border-gray-200 shrink-0">
            <div />
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">Category</p>
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">Duration</p>
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide text-right">%</p>
          </div>

          <div className="flex-1 overflow-y-auto min-h-0">
            {isLoading ? (
              <p className="text-sm text-gray-600 py-12 text-center animate-pulse">Loading…</p>
            ) : filtered.length === 0 ? (
              <p className="text-sm text-gray-600 py-12 text-center">No categories found.</p>
            ) : paged.map(item => (
              <button
                key={item.name}
                onClick={() => setSelected(prev => prev?.name === item.name ? null : item)}
                className={`w-full grid grid-cols-[16px_1fr_110px_52px] sm:grid-cols-[16px_1fr_120px_60px] gap-3 px-4 sm:px-5 py-3.5 border-b border-gray-100 hover:bg-brand-50/40 transition text-left ${
                  selected?.name === item.name ? "bg-brand-50" : ""
                }`}
              >
                <div className="w-3 h-3 rounded-full mt-0.5 shrink-0" style={{ background: item.color }} />
                <div className="min-w-0">
                  <p className="text-sm font-medium text-gray-800">{item.name}</p>
                  <p className="text-xs text-gray-600">{item.user_count} users</p>
                </div>
                <p className="text-sm text-gray-700 tabular-nums self-center">{fmtDur(item.duration_secs)}</p>
                <p className="text-sm text-gray-600 tabular-nums text-right self-center">{item.pct.toFixed(1)}%</p>
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
            <DetailPanel item={selected} since={dateRange.since} until={dateRange.until} groupId={groupId} />
          </div>
        )}
      </div>

      {!selected && filtered.length > 0 && (
        <p className="text-xs text-gray-600 text-center mt-2 shrink-0">
          Tap a category to see activities and users
        </p>
      )}
    </div>
  );
}
