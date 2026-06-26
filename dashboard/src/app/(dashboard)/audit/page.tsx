"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useCallback } from "react";
import useSWR from "swr";
import { ClipboardList, Search, ChevronLeft, ChevronRight, ChevronDown, ChevronUp } from "lucide-react";
import { getAudit, getAuditFilterOptions, type AuditEntry, type AuditFilters } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";

const ACTION_COLOR: Record<string, string> = {
  create:  "bg-green-50 text-green-700 ring-1 ring-green-200",
  delete:  "bg-red-50 text-red-700 ring-1 ring-red-200",
  deactivate: "bg-red-50 text-red-700 ring-1 ring-red-200",
  update:  "bg-blue-50 text-blue-700 ring-1 ring-blue-200",
  activate: "bg-amber-50 text-amber-700 ring-1 ring-amber-200",
  upload:  "bg-purple-50 text-purple-700 ring-1 ring-purple-200",
  approve: "bg-teal-50 text-teal-700 ring-1 ring-teal-200",
  assign:  "bg-indigo-50 text-indigo-700 ring-1 ring-indigo-200",
  unassigned: "bg-gray-100 text-gray-600 ring-1 ring-gray-200",
  register: "bg-cyan-50 text-cyan-700 ring-1 ring-cyan-200",
  add:     "bg-green-50 text-green-700 ring-1 ring-green-200",
  remove:  "bg-orange-50 text-orange-700 ring-1 ring-orange-200",
};

function actionStyle(action: string) {
  const verb = action.split(".").pop() ?? "";
  return ACTION_COLOR[verb] ?? "bg-gray-100 text-gray-600 ring-1 ring-gray-200";
}

function formatAction(action: string) {
  return action.replace(/\./g, " › ");
}

const PAGE_SIZE = 50;

function MetadataCell({ entry }: { entry: AuditEntry }) {
  const [expanded, setExpanded] = useState(false);
  if (!entry.metadata) return <span className="text-gray-500">—</span>;
  const text = JSON.stringify(entry.metadata, null, 2);
  const preview = JSON.stringify(entry.metadata);

  return (
    <div className="max-w-xs">
      {expanded ? (
        <div>
          <pre className="text-xs bg-gray-50 border border-gray-200 rounded p-2 overflow-auto max-h-48 whitespace-pre-wrap break-all">{text}</pre>
          <button onClick={() => setExpanded(false)} className="mt-1 text-xs text-brand-600 hover:underline flex items-center gap-0.5">
            <ChevronUp size={12} /> Collapse
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-1">
          <span className="text-xs text-gray-600 truncate max-w-[180px]">{preview}</span>
          <button onClick={() => setExpanded(true)} className="text-xs text-brand-600 hover:underline flex items-center gap-0.5 shrink-0">
            <ChevronDown size={12} /> More
          </button>
        </div>
      )}
    </div>
  );
}

export default function AuditPage() {
  const { loading, can } = useUser();

  const [filters, setFilters] = useState<AuditFilters>({ limit: PAGE_SIZE, offset: 0 });
  const [draftSearch, setDraftSearch] = useState("");

  const swrKey = JSON.stringify({ ...filters, _: "audit" });
  const { data, isLoading } = useSWR(
    can("audit", "view") ? swrKey : null,
    () => getAudit(filters),
    { refreshInterval: 30_000, keepPreviousData: true }
  );

  const { data: filterOpts } = useSWR(
    can("audit", "view") ? "audit-filters" : null,
    getAuditFilterOptions,
    { revalidateOnFocus: false }
  );

  const setFilter = useCallback((key: keyof AuditFilters, value: string | undefined) => {
    setFilters(prev => ({ ...prev, [key]: value || undefined, offset: 0 }));
  }, []);

  const commitSearch = () => setFilter("search", draftSearch || undefined);
  const clearAll = () => {
    setFilters({ limit: PAGE_SIZE, offset: 0 });
    setDraftSearch("");
  };

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 0;
  const currentPage = data ? Math.floor((data.offset ?? 0) / PAGE_SIZE) + 1 : 1;
  const hasFilters = !!(filters.action || filters.target_type || filters.actor_id || filters.since || filters.until || filters.search);

  if (loading) return <PageLoader />;
  if (!can("audit", "view")) return <AccessDenied label="the Audit Log" />;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Audit Log</h1>
          <p className="text-gray-600 text-sm mt-0.5">
            Every administrative action across the organization
            {data && <span className="ml-2 font-medium text-gray-700">{data.total.toLocaleString()} total entries</span>}
          </p>
        </div>
      </div>

      {/* Filters bar */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm space-y-3">
        {/* Search row */}
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
            <input
              type="text"
              placeholder="Search actions, actors, targets, metadata…"
              value={draftSearch}
              onChange={e => setDraftSearch(e.target.value)}
              onKeyDown={e => e.key === "Enter" && commitSearch()}
              className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-300"
            />
          </div>
          <button
            onClick={commitSearch}
            className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 font-medium"
          >
            Search
          </button>
          {hasFilters && (
            <button onClick={clearAll} className="px-3 py-2 text-sm text-gray-600 hover:text-gray-700 border border-gray-200 rounded-lg">
              Clear
            </button>
          )}
        </div>

        {/* Filter dropdowns */}
        <div className="flex flex-wrap gap-2">
          {/* Action filter */}
          <select
            value={filters.action ?? ""}
            onChange={e => setFilter("action", e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-brand-300 bg-white min-w-[160px]"
          >
            <option value="">All actions</option>
            {filterOpts?.actions.map(a => (
              <option key={a} value={a}>{formatAction(a)}</option>
            ))}
          </select>

          {/* Target type filter */}
          <select
            value={filters.target_type ?? ""}
            onChange={e => setFilter("target_type", e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-brand-300 bg-white min-w-[140px]"
          >
            <option value="">All targets</option>
            {filterOpts?.target_types.map(t => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>

          {/* Actor filter */}
          <select
            value={filters.actor_id ?? ""}
            onChange={e => setFilter("actor_id", e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-brand-300 bg-white min-w-[160px]"
          >
            <option value="">All actors</option>
            {filterOpts?.actors.map(a => (
              <option key={a.id} value={a.id}>{a.name || a.email}</option>
            ))}
          </select>

          {/* Date range */}
          <div className="flex items-center gap-1">
            <span className="text-xs text-gray-600 shrink-0">From</span>
            <input
              type="date"
              value={filters.since ?? ""}
              onChange={e => setFilter("since", e.target.value)}
              className="text-sm border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-brand-300"
            />
          </div>
          <div className="flex items-center gap-1">
            <span className="text-xs text-gray-600 shrink-0">To</span>
            <input
              type="date"
              value={filters.until ?? ""}
              onChange={e => setFilter("until", e.target.value)}
              className="text-sm border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-brand-300"
            />
          </div>
        </div>

        {/* Active filter chips */}
        {hasFilters && (
          <div className="flex flex-wrap gap-1.5">
            {filters.action && (
              <span className="inline-flex items-center gap-1 text-xs bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">
                Action: {formatAction(filters.action)}
                <button onClick={() => setFilter("action", undefined)} className="hover:text-brand-900">×</button>
              </span>
            )}
            {filters.target_type && (
              <span className="inline-flex items-center gap-1 text-xs bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">
                Target: {filters.target_type}
                <button onClick={() => setFilter("target_type", undefined)} className="hover:text-brand-900">×</button>
              </span>
            )}
            {filters.actor_id && (
              <span className="inline-flex items-center gap-1 text-xs bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">
                Actor: {filterOpts?.actors.find(a => a.id === filters.actor_id)?.name ?? filters.actor_id}
                <button onClick={() => setFilter("actor_id", undefined)} className="hover:text-brand-900">×</button>
              </span>
            )}
            {filters.since && (
              <span className="inline-flex items-center gap-1 text-xs bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">
                From: {filters.since}
                <button onClick={() => setFilter("since", undefined)} className="hover:text-brand-900">×</button>
              </span>
            )}
            {filters.until && (
              <span className="inline-flex items-center gap-1 text-xs bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">
                To: {filters.until}
                <button onClick={() => setFilter("until", undefined)} className="hover:text-brand-900">×</button>
              </span>
            )}
            {filters.search && (
              <span className="inline-flex items-center gap-1 text-xs bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">
                Search: &quot;{filters.search}&quot;
                <button onClick={() => { setFilter("search", undefined); setDraftSearch(""); }} className="hover:text-brand-900">×</button>
              </span>
            )}
          </div>
        )}
      </div>

      {/* Table */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[780px]">
            <thead>
              <tr className="bg-gray-50 text-gray-600 text-xs uppercase tracking-wide border-b border-gray-100">
                <th className="text-left px-4 py-3 font-semibold w-36">When</th>
                <th className="text-left px-4 py-3 font-semibold w-44">Actor</th>
                <th className="text-left px-4 py-3 font-semibold w-48">Action</th>
                <th className="text-left px-4 py-3 font-semibold w-24">Target</th>
                <th className="text-left px-4 py-3 font-semibold">Details</th>
              </tr>
            </thead>
            <tbody>
              {isLoading && !data && (
                <tr>
                  <td colSpan={5} className="px-4 py-12 text-center text-gray-600 text-sm">
                    Loading…
                  </td>
                </tr>
              )}
              {data?.entries.map(e => (
                <tr key={e.id} className="border-t border-gray-50 hover:bg-gray-50/80 transition-colors">
                  <td className="px-4 py-3 text-xs text-gray-600 whitespace-nowrap align-top">
                    {e.created_at ? (
                      <div>
                        <div>{new Date(e.created_at).toLocaleDateString()}</div>
                        <div className="text-gray-600">{new Date(e.created_at).toLocaleTimeString()}</div>
                      </div>
                    ) : "—"}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <div className="font-medium text-gray-800 text-xs">{e.actor_name ?? <span className="text-gray-600">—</span>}</div>
                    {e.actor_email && <div className="text-gray-600 text-xs">{e.actor_email}</div>}
                    {e.actor_role && <div className="text-xs text-gray-600 mt-0.5 italic">{e.actor_role}</div>}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${actionStyle(e.action)}`}>
                      {formatAction(e.action)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-600 align-top">
                    {e.target_type ? (
                      <div>
                        <div className="font-medium">{e.target_type}</div>
                        {e.target_id && <div className="text-gray-600 font-mono text-[10px] truncate max-w-[80px]" title={e.target_id}>{e.target_id.slice(0, 8)}…</div>}
                      </div>
                    ) : "—"}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <MetadataCell entry={e} />
                  </td>
                </tr>
              ))}
              {data?.entries.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-12 text-center text-gray-600 text-sm">
                    <ClipboardList size={28} className="mx-auto mb-2 text-gray-200" />
                    {hasFilters ? "No entries match the current filters." : "No audit entries yet."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {data && data.total > PAGE_SIZE && (
          <div className="border-t border-gray-100 px-4 py-3 flex items-center justify-between">
            <span className="text-xs text-gray-600">
              Showing {(data.offset ?? 0) + 1}–{Math.min((data.offset ?? 0) + PAGE_SIZE, data.total)} of {data.total.toLocaleString()}
            </span>
            <div className="flex items-center gap-1">
              <button
                disabled={currentPage <= 1}
                onClick={() => setFilters(p => ({ ...p, offset: Math.max(0, (p.offset ?? 0) - PAGE_SIZE) }))}
                className="p-1.5 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronLeft size={14} />
              </button>
              <span className="text-xs text-gray-600 px-2">Page {currentPage} of {totalPages}</span>
              <button
                disabled={currentPage >= totalPages}
                onClick={() => setFilters(p => ({ ...p, offset: (p.offset ?? 0) + PAGE_SIZE }))}
                className="p-1.5 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
