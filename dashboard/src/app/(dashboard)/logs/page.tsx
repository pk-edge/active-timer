"use client";
import PageLoader from "@/components/PageLoader";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import useSWR, { preload } from "swr";
import { format } from "date-fns";
import {
  Monitor, Globe, Lock, MousePointer,
  Search, X, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { getLogs, getDevices, getUsers, fmtTime, fmtSeconds } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { smartSort } from "@/lib/smartSearch";
import { AccessDenied } from "@/components/AccessDenied";
import DateRangePicker, { type DateRange } from "@/components/DateRangePicker";
import TalentSearchBar, { type TalentSearchResult } from "@/components/TalentSearchBar";
import type { LogEvent, Device, User } from "@/types";

const EVENT_ICON: Record<string, React.ReactNode> = {
  window_change: <Monitor      size={13} />,
  url_visit:     <Globe        size={13} />,
  lock:          <Lock         size={13} />,
  unlock:        <Lock         size={13} />,
  idle_start:    <MousePointer size={13} />,
  idle_end:      <MousePointer size={13} /> };

const EVENT_COLOR: Record<string, string> = {
  window_change: "bg-brand-50 text-brand-700 border-brand-200",
  url_visit:     "bg-purple-50 text-purple-700 border-purple-200",
  lock:          "bg-red-50 text-red-700 border-red-200",
  unlock:        "bg-green-50 text-green-700 border-green-200",
  idle_start:    "bg-gray-100 text-gray-600 border-gray-200",
  idle_end:      "bg-gray-50 text-gray-600 border-gray-200" };

const EVENT_ROW_BG: Record<string, string> = {
  window_change: "hover:bg-brand-50/40",
  url_visit:     "hover:bg-purple-50/40",
  lock:          "hover:bg-red-50/30",
  unlock:        "hover:bg-green-50/30",
  idle_start:    "hover:bg-gray-50",
  idle_end:      "hover:bg-gray-50" };

const EVENT_LABEL: Record<string, string> = {
  window_change: "App switch",
  url_visit:     "URL visit",
  lock:          "Locked",
  unlock:        "Unlocked",
  idle_start:    "Went idle",
  idle_end:      "Back" };

const EVENT_TYPES = [
  "window_change", "url_visit", "lock", "unlock", "idle_start", "idle_end",
];

// Matches the old client-side page size — now a real server-side page.
const PAGE_SIZE = 50;

function EventBadge({ type }: { type: string }) {
  const cls = EVENT_COLOR[type] ?? "bg-gray-100 text-gray-600 border-gray-200";
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full border ${cls}`}>
      {EVENT_ICON[type]}
      {EVENT_LABEL[type] ?? type}
    </span>
  );
}

function LogRow({ e }: { e: LogEvent }) {
  const rowBg = EVENT_ROW_BG[e.event_type] ?? "hover:bg-gray-50";
  return (
    <tr className={`border-b border-gray-100 transition ${rowBg}`}>
      <td className="py-3 px-4 text-xs text-gray-600 whitespace-nowrap font-mono">
        {fmtTime(e.timestamp_unix)}
      </td>
      <td className="py-3 px-4 whitespace-nowrap">
        <EventBadge type={e.event_type} />
      </td>
      <td className="py-3 px-4 max-w-sm">
        <div className="flex flex-col gap-0.5">
          {e.app_name && (
            <span className="text-sm font-medium text-gray-800 truncate">{e.app_name}</span>
          )}
          {e.window_title && (
            <span className="text-xs text-gray-600 truncate" title={e.window_title}>
              {e.window_title}
            </span>
          )}
          {e.url && (
            <span className="text-xs text-purple-500 truncate" title={e.url}>
              {e.url}
            </span>
          )}
          {!e.app_name && !e.window_title && !e.url && (
            <span className="text-xs text-gray-600 italic">{e.event_type}</span>
          )}
        </div>
      </td>
      <td className="py-3 px-4 text-xs text-gray-600 whitespace-nowrap">
        {e.duration > 0
          ? <span className="bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full font-medium">{fmtSeconds(e.duration)}</span>
          : <span className="text-gray-500">—</span>}
      </td>
      <td className="py-3 px-4 text-xs whitespace-nowrap">
        {e.employee_name
          ? <span className="font-medium text-gray-700">{e.employee_name}</span>
          : <span className="text-gray-500">—</span>}
      </td>
      <td className="py-3 px-4 text-xs whitespace-nowrap">
        <Link
          href={`/devices/${e.device_id}`}
          className="text-gray-600 hover:text-brand-600 transition font-medium"
        >
          {e.hostname}
        </Link>
      </td>
    </tr>
  );
}

/** Local-midnight-of-day → unix seconds, matching the math the backend has
 *  always been given (single-day math generalized to a range). */
function dayStartUnix(yyyyMmDd: string): number {
  return Math.floor(new Date(yyyyMmDd + "T00:00:00").getTime() / 1000);
}

/** Cosmetic only — the agent reports raw sys.platform values ("darwin",
 *  "win32"), which vary in casing across the codebase; normalize just for
 *  this detail line rather than reaching for a shared util. */
function platformLabel(p: string): string {
  const v = p.toLowerCase();
  if (v === "darwin" || v === "macos") return "macOS";
  if (v === "win32" || v === "windows") return "Windows";
  if (v === "linux") return "Linux";
  return p;
}

export default function LogsPage() {
  const { loading, can } = useUser();
  const today = format(new Date(), "yyyy-MM-dd");

  // DateRangePicker gives Weekly/Monthly/Quarterly-equivalent presets (Last 7
  // Days, This Month, Last 90 Days, etc.) plus a custom range — default of
  // {today, today} reproduces the old single-day-only behavior exactly.
  const [dateRange, setDateRange] = useState<DateRange>({ since: today, until: today });
  const [selDevice, setSelDevice] = useState("");
  const [selUser,   setSelUser]   = useState("");
  const [selType,   setSelType]   = useState("");
  const [search,    setSearch]    = useState("");
  const [talentQuery, setTalentQuery] = useState("");
  const [deviceQuery, setDeviceQuery] = useState("");
  const [offset,    setOffset]    = useState(0);

  function resetToFirstPage() { setOffset(0); }

  // Devices already power the working "Device" filter today (this page only
  // requires logs:view, and GET /v1/devices needs devices:view — if that were
  // broken the existing selector would already be empty, so it isn't the bug
  // here). Users, however, ARE the bug: GET /v1/users needs users:view, which
  // a logs:view-only role need not hold, and the failed fetch silently
  // collapsed the "User" dropdown to just "All users". Fetch users only when
  // permitted, and derive a working talent list from `devices` either way so
  // the filter and search bar are never empty just because of that permission.
  const canViewUsers = can("users", "view");
  const { data: devices } = useSWR<Device[]>("devices", getDevices);
  const { data: users }   = useSWR<User[]>(canViewUsers ? "users-list" : null, () => getUsers());

  // One row per PERSON (not per device): group devices by employee_id so a
  // talent with two machines appears once, enriched with their email when
  // `users:view` is held. This is what both the "User" select and the new
  // search bar are built from — it directly fixes "no filter for selected
  // users" (previously empty for many roles) and "unable to search talent by
  // device" (previously keyed on device, so similarly-named people collided).
  const talentPeople = useMemo(() => {
    const byId = new Map<string, { id: string; name: string; email: string; hostnames: string[] }>();
    for (const d of devices ?? []) {
      if (!d.employee_id || !d.employee_name) continue;
      const existing = byId.get(d.employee_id);
      if (existing) existing.hostnames.push(d.hostname);
      else byId.set(d.employee_id, { id: d.employee_id, name: d.employee_name, email: "", hostnames: [d.hostname] });
    }
    for (const u of users ?? []) {
      const existing = byId.get(u.id);
      if (existing) existing.email = u.email;
      // A user with no device can never match a log row (the backend filters
      // by the DEVICE's assigned employee), so they're intentionally omitted
      // rather than offered as a dead-end filter option.
    }
    return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [devices, users]);

  const debouncedTalentQuery = useDebouncedValue(talentQuery, 200);
  const talentResults: TalentSearchResult[] = useMemo(
    () => smartSort(
      debouncedTalentQuery, talentPeople, p => [p.name, p.email, ...p.hostnames],
    ).map(p => ({
      id: p.id,
      name: p.name,
      email: p.email || p.hostnames.join(", "),
      detail: p.email ? p.hostnames.join(", ") : null,
    })),
    [debouncedTalentQuery, talentPeople],
  );

  function handleSelectTalent(result: TalentSearchResult) {
    setSelUser(result.id);
    setSelDevice("");
    setTalentQuery(result.name);
    resetToFirstPage();
  }

  // Keep the search bar's text in sync whenever selUser changes from
  // elsewhere — defensive, since nothing else currently sets it besides
  // handleSelectTalent and this box's own clear-to-empty.
  useEffect(() => {
    if (!selUser) { setTalentQuery(""); return; }
    const match = talentPeople.find(p => p.id === selUser);
    if (match) setTalentQuery(match.name);
  }, [selUser, talentPeople]);

  // Device search bar — a proper searchable picker (hostname/employee/
  // platform) in place of the old plain <select>, which only offered
  // first-letter browser type-ahead over a concatenated "employee — hostname"
  // string. Mirrors the talent search bar's pattern exactly.
  const debouncedDeviceQuery = useDebouncedValue(deviceQuery, 200);
  const deviceResults: TalentSearchResult[] = useMemo(
    () => smartSort(
      debouncedDeviceQuery, devices ?? [], d => [d.hostname, d.employee_name, d.platform],
    ).map(d => ({
      id: d.id,
      name: d.hostname,
      email: d.employee_name ?? "Unassigned",
      detail: platformLabel(d.platform),
    })),
    [debouncedDeviceQuery, devices],
  );

  function handleSelectDevice(result: TalentSearchResult) {
    setSelDevice(result.id);
    setDeviceQuery(result.name);
    resetToFirstPage();
  }

  // Keep the device box's text in sync whenever selDevice changes from
  // elsewhere — in particular, selecting a talent above clears selDevice, and
  // this box must not keep showing a hostname for a filter no longer applied.
  useEffect(() => {
    if (!selDevice) { setDeviceQuery(""); return; }
    const match = devices?.find(d => d.id === selDevice);
    if (match) setDeviceQuery(match.hostname);
  }, [selDevice, devices]);

  const since = dayStartUnix(dateRange.since);
  // Exclusive upper bound: local midnight AFTER the "until" day, so the until
  // day itself is fully included — same convention the single-day version
  // used (since + 86400).
  const until = dayStartUnix(dateRange.until) + 86400;

  // Free-text search (app/window/url/user/hostname) now runs SERVER-SIDE over
  // the FULL matching date-range/device/user-scoped dataset (see
  // `backend/app/api/v1/endpoints/logs.py`'s `search` param), not just
  // whatever rows happen to be on the current page — pagination sits ON TOP
  // of the search, narrowing the candidate set in SQL first and then paging
  // through exactly that narrowed set. Debounced so typing doesn't fire a
  // request per keystroke. The talent search bar above remains the tool for
  // finding a specific PERSON across the whole range (it sets employee_id).
  const debouncedSearch = useDebouncedValue(search, 200);

  // Any filter change (including a debounced search term change) starts back
  // at page 1 — same pattern as every other filter on this page.
  useEffect(() => {
    resetToFirstPage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch]);

  const logsOpts = {
    since, until,
    device_id:   selDevice || undefined,
    employee_id: selUser || undefined,
    event_type:  selType || undefined,
    search:      debouncedSearch || undefined,
    limit:  PAGE_SIZE,
    offset,
  };

  const swrKey = ["logs", since, until, selDevice, selUser, selType, debouncedSearch, offset] as const;

  const { data: page, isLoading } = useSWR(
    swrKey,
    () => getLogs(logsOpts),
    { refreshInterval: 15_000, keepPreviousData: true },
  );

  // The items returned ARE already the filtered set (server-side) — render
  // them directly, no client-side re-filtering (that would be redundant and
  // could subtly diverge from the server's build_search() semantics).
  const items = page?.items ?? [];

  // Background-prefetch the next page once the current one has loaded and
  // more is available, so clicking "Next" feels instant. The key here MUST
  // match the main useSWR key shape exactly (same values, same order) except
  // for the offset, so that when the user clicks Next and `offset` state
  // updates to `nextOffset`, the main call hits this warm cache instead of
  // re-fetching.
  useEffect(() => {
    if (!page?.has_more) return;
    const nextOffset = offset + PAGE_SIZE;
    preload(
      ["logs", since, until, selDevice, selUser, selType, debouncedSearch, nextOffset],
      () => getLogs({ ...logsOpts, offset: nextOffset }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, since, until, selDevice, selUser, selType, debouncedSearch, offset]);

  // Event-type counts reflect this page only (an exact count across the whole
  // range would need the same unbounded COUNT(*) the backend deliberately
  // avoids) — labelled as such rather than implying a range-wide total.
  const counts = items.reduce<Record<string, number>>((acc, e) => {
    acc[e.event_type] = (acc[e.event_type] ?? 0) + 1;
    return acc;
  }, {});

  if (loading) return <PageLoader />;
  if (!can("logs", "view")) return <AccessDenied label="Activity Logs" />;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1 space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6 shrink-0">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Activity Logs</h1>
          <p className="text-gray-600 text-sm mt-0.5">Live feed — refreshes every 15 s</p>
        </div>
      </div>

      {/* Talent search — finds a PERSON across the whole date range */}
      <div className="shrink-0">
        <TalentSearchBar
          value={talentQuery}
          onChange={v => { setTalentQuery(v); if (!v) { setSelUser(""); resetToFirstPage(); } }}
          results={talentResults}
          onSelect={handleSelectTalent}
          placeholder="Find a talent by name or email…"
          label="Find a talent"
          className="max-w-md"
        />
      </div>

      {/* Filters row */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 mb-5 shadow-sm shrink-0">
        <div className="flex flex-wrap gap-3 items-center">
          {/* Date range */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-600 font-medium">Date range</label>
            <DateRangePicker
              value={dateRange}
              onChange={r => { setDateRange(r); resetToFirstPage(); }}
            />
          </div>

          {/* Device — proper search bar, not a runtime-search dropdown */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-600 font-medium">Device</label>
            <TalentSearchBar
              value={deviceQuery}
              onChange={v => { setDeviceQuery(v); if (!v) { setSelDevice(""); resetToFirstPage(); } }}
              results={deviceResults}
              onSelect={handleSelectDevice}
              placeholder="All devices — search hostname…"
              label="Find a device"
              className="w-64 shrink-0"
            />
          </div>

          {/* No separate "User" control — the "Find a talent" search bar above
              already does this job with a proper search bar (see its own
              onSelect), so a second dropdown for the same filter would just
              be a redundant, worse (plain-<select>) control. */}

          {/* Event type */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-600 font-medium">Event type</label>
            <div className="relative">
              <select
                value={selType}
                onChange={e => { setSelType(e.target.value); resetToFirstPage(); }}
                className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
              >
                <option value="">All types</option>
                {EVENT_TYPES.map(t => (
                  <option key={t} value={t}>{EVENT_LABEL[t] ?? t}</option>
                ))}
              </select>
              <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
            </div>
          </div>

          {/* Free-text event search — full date-range/device/user-scoped
              dataset, server-side, see note above */}
          <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
            <label className="text-xs text-gray-600 font-medium">Search</label>
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
              <input
                type="text"
                placeholder="App, window title, URL, user, hostname…"
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full pl-9 pr-8 py-2 bg-white border border-gray-300 rounded-lg text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
              />
              {search && (
                <button
                  onClick={() => setSearch("")}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600"
                >
                  <X size={13} />
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Event count summary chips — this page only */}
        {Object.keys(counts).length > 0 && (
          <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t border-gray-100">
            {Object.entries(counts)
              .sort((a, b) => b[1] - a[1])
              .map(([type, count]) => (
                <button
                  key={type}
                  onClick={() => { setSelType(selType === type ? "" : type); resetToFirstPage(); }}
                  className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border transition ${
                    selType === type
                      ? EVENT_COLOR[type] ?? "bg-brand-50 text-brand-700 border-brand-200"
                      : "bg-gray-50 text-gray-600 border-gray-200 hover:border-gray-300"
                  }`}
                >
                  {EVENT_LABEL[type] ?? type}
                  <span className="font-semibold">{count}</span>
                </button>
              ))}
            <span className="text-xs text-gray-600 ml-auto self-center">
              {items.length} event{items.length !== 1 ? "s" : ""} on this page
            </span>
          </div>
        )}
      </div>

      {/* Table */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        {isLoading ? (
          <div className="py-16 text-center text-gray-600 text-sm animate-pulse">Loading activity…</div>
        ) : !items.length ? (
          <div className="py-16 text-center text-gray-600 text-sm">
            <Search size={32} className="mx-auto mb-3 text-gray-200" />
            {debouncedSearch ? "No events match your search." : "No activity found for the selected filters."}
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-200">
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-600 uppercase tracking-wide whitespace-nowrap">Time</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-600 uppercase tracking-wide whitespace-nowrap">Type</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-600 uppercase tracking-wide">Activity</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-600 uppercase tracking-wide whitespace-nowrap">Duration</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-600 uppercase tracking-wide whitespace-nowrap">User</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-600 uppercase tracking-wide whitespace-nowrap">Device</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map(e => <LogRow key={e.id} e={e} />)}
                </tbody>
              </table>
            </div>

            {/* Server-side pagination — no exact total is available (see
                LogsPage doc comment), so this is Prev/Next with an honest
                "more available" signal rather than a fabricated page count. */}
            <div className="border-t border-gray-100 px-4 py-3 flex items-center justify-between">
              <span className="text-xs text-gray-600">
                Showing {offset + 1}–{offset + items.length}
                {page?.has_more ? " · more available" : ""}
              </span>
              <div className="flex items-center gap-1">
                <button
                  disabled={offset <= 0}
                  onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
                  className="p-1.5 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <ChevronLeft size={14} />
                </button>
                <button
                  disabled={!page?.has_more}
                  onClick={() => setOffset(o => o + PAGE_SIZE)}
                  className="p-1.5 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
