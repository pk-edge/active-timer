"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useMemo, useEffect } from "react";
import dynamic from "next/dynamic";
import useSWR from "swr";
import { format, subDays, differenceInCalendarDays } from "date-fns";
import type { ReportDateRange } from "@/lib/api";
import {
  getCompanySummary, getTopApps, getDevices, getUsers,
  getDeviceSummary, getDeviceTopApps, fmtSeconds,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { smartSort } from "@/lib/smartSearch";
import { platformLabel } from "@/lib/devicePlatform";
import TalentSearchBar, { type TalentSearchResult } from "@/components/TalentSearchBar";
import { AccessDenied } from "@/components/AccessDenied";
import DateRangePicker, { type DateRange } from "@/components/DateRangePicker";
import ExportReportDialog from "@/components/ExportReportDialog";
import { normalizeReportRange, utcToday } from "@/lib/reportRange";

// Lazy: the per-day bar chart pulls in recharts only when it actually renders.
const DailyActiveBarChart = dynamic(() => import("./_components/DailyActiveBarChart"), {
  ssr: false,
  loading: () => (
    <div className="h-32 sm:h-40 animate-pulse flex items-end gap-2">
      {[55, 80, 40, 90, 65, 50, 75].map((p, i) => (
        <div key={i} className="flex-1 bg-gray-100 rounded-t" style={{ height: `${p}%` }} />
      ))}
    </div>
  ),
});
import { Download, Search, X } from "lucide-react";
import type { CompanyDay, AppUsage, Device, User } from "@/types";

// Company-wide top-apps caps at 100 server-side (backend/app/api/v1/endpoints/
// reports.py). Ask for the max so the in-card search box has more than the
// top 20 to search over — otherwise anything ranked below #20 is invisible
// to the search box and looks like a false "no match".
const APP_SEARCH_LIMIT = 100;

// Formats a DateRange for the header subtitle / stat card — honest about the
// ACTUAL active range instead of a hardcoded "last N days" that used to be
// true by construction (days-only) but no longer is once Custom/Last Month/
// Last Year ranges are possible.
function describeRange(range: DateRange): string {
  return range.since === range.until ? range.since : `${range.since} – ${range.until}`;
}

// Company summary/top-apps are two of the heaviest dashboard endpoints, which
// is why the backend keeps a pre-aggregated per-day cache. A range ending
// today that fits the `days` model (≤90 days) is sent as plain `days`: the
// server resolves `days=N` to exactly the calendar days [today-(N-1), today]
// (UTC) — the same rows as the equivalent since/until, and the shape its
// top-apps cache is keyed by. Anything else (a past end date, >90 days) is
// sent as since/until. Either way the window equals what "Export report"
// covers for the same range, so the numbers match. `range` must already be
// normalized (normalizeReportRange) so "today" means the server's UTC today.
function reportRangeArg(range: DateRange): { days: number; range?: ReportDateRange } {
  const days = differenceInCalendarDays(new Date(range.until), new Date(range.since)) + 1;
  if (range.until === utcToday() && days <= 90) {
    return { days };
  }
  return { days, range };
}

export default function ReportsPage() {
  const { loading, can } = useUser();
  const [dateRange, setDateRange] = useState<DateRange>(() => ({
    since: format(subDays(new Date(), 6), "yyyy-MM-dd"),
    until: format(new Date(), "yyyy-MM-dd"),
  }));
  const rangeDays = differenceInCalendarDays(new Date(dateRange.until), new Date(dateRange.since)) + 1;
  const [deviceId, setDeviceId] = useState("");
  const [appSearch, setAppSearch] = useState("");
  const [talentQuery, setTalentQuery] = useState("");
  const [deviceQuery, setDeviceQuery] = useState("");
  const [exportOpen, setExportOpen] = useState(false);
  const debouncedAppSearch = useDebouncedValue(appSearch, 200);

  const { data: devices } = useSWR<Device[]>("devices", getDevices);
  const selectedDevice = devices?.find(d => d.id === deviceId);

  // Devices are gated on `reports:view` (this page's own permission), but
  // employee email lives behind `users:view` — a separate module. Fetch it
  // only when the caller actually holds that permission; unconditionally
  // calling getUsers() here would 403 for any reports-only role. When it's
  // not available, the talent search bar falls back to hostname(s) alone as
  // the disambiguator, which the PRD explicitly allows ("email or devices").
  const canViewUsers = can("users", "view");
  const { data: users } = useSWR<User[]>(canViewUsers ? "reports-talent-emails" : null, () => getUsers());
  const emailByEmployeeId = useMemo(() => {
    const map = new Map<string, string>();
    users?.forEach(u => map.set(u.id, u.email));
    return map;
  }, [users]);

  // The only person-shaped data this page has is `devices`, which is
  // device-keyed: a talent with two laptops appears twice, and an unassigned
  // device has no person at all. Group by employee_id so each PERSON shows up
  // once in the search results (with their device hostnames folded into the
  // detail line) instead of once per device — that's what actually resolves
  // "three Mohammads" into distinct, pickable rows. Unassigned devices have no
  // employee to group under, so they're deliberately left out of this list;
  // they remain reachable exactly as before via the plain `<select>` below.
  const talentPeople = useMemo(() => {
    const map = new Map<string, { id: string; name: string; deviceIds: string[]; hostnames: string[] }>();
    for (const d of devices ?? []) {
      if (!d.employee_id) continue;
      const existing = map.get(d.employee_id);
      if (existing) {
        existing.deviceIds.push(d.id);
        existing.hostnames.push(d.hostname);
      } else {
        map.set(d.employee_id, {
          id: d.employee_id,
          name: d.employee_name ?? d.hostname,
          deviceIds: [d.id],
          hostnames: [d.hostname],
        });
      }
    }
    return Array.from(map.values());
  }, [devices]);

  const talentResults: TalentSearchResult[] = useMemo(() => {
    return smartSort(
      talentQuery,
      talentPeople,
      p => [p.name, emailByEmployeeId.get(p.id), ...p.hostnames],
    ).map(p => {
      const email = emailByEmployeeId.get(p.id);
      const hostList = p.hostnames.join(", ");
      // Email + hostname together when we have both (the PRD's ideal
      // disambiguator); hostname alone stands in for email when `users:view`
      // isn't held.
      return email
        ? { id: p.id, name: p.name, email, detail: hostList }
        : { id: p.id, name: p.name, email: hostList };
    });
  }, [talentQuery, talentPeople, emailByEmployeeId]);

  function handleSelectTalent(result: TalentSearchResult) {
    const person = talentPeople.find(p => p.id === result.id);
    if (person && person.deviceIds.length > 0) {
      // A person with multiple devices lands on their first one; the device
      // search bar below still finds every individual device, so switching
      // to a specific one of their other machines stays possible.
      setDeviceId(person.deviceIds[0]);
    }
    setTalentQuery("");
  }

  // Device search bar — a proper searchable picker (hostname/employee/
  // platform), in place of the old plain "All users / devices" <select>,
  // which only offered first-letter browser type-ahead over a concatenated
  // string. Lists every device individually (including unassigned ones,
  // which the talent search above deliberately omits), so it's still the
  // tool for picking a specific machine rather than a person.
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
    setDeviceId(result.id);
    setDeviceQuery(result.name);
  }

  // Keep both search boxes' text in sync whenever deviceId changes from
  // elsewhere (picking a talent above sets deviceId to their first device;
  // clearing either box clears deviceId directly) — neither box should keep
  // showing stale text for a filter that's no longer actually applied.
  useEffect(() => {
    if (!deviceId) { setDeviceQuery(""); return; }
    const match = devices?.find(d => d.id === deviceId);
    if (match) setDeviceQuery(match.hostname);
  }, [deviceId, devices]);

  // What the server actually reports on (picker dates shifted to the UTC
  // day when the local date is ahead) — drives the queries, the subtitle and
  // the export's initial range, so all three describe the same window.
  const effectiveRange = useMemo(() => normalizeReportRange(dateRange), [dateRange]);
  const { days: rangeArgDays, range: rangeArg } = reportRangeArg(effectiveRange);

  const summaryKey = deviceId
    ? ["device-summary", deviceId, effectiveRange.since, effectiveRange.until]
    : ["summary", effectiveRange.since, effectiveRange.until];
  const appsKey = deviceId
    ? ["device-apps", deviceId, effectiveRange.since, effectiveRange.until]
    : ["top-apps", effectiveRange.since, effectiveRange.until];

  const { data: summary, isLoading: summaryLoading, isValidating: summaryValidating } = useSWR<CompanyDay[]>(
    summaryKey,
    () => deviceId
      ? getDeviceSummary(deviceId, rangeArgDays, rangeArg)
      : getCompanySummary(rangeArgDays, rangeArg),
  );
  const { data: apps, isLoading: appsLoading, isValidating: appsValidating } = useSWR<AppUsage[]>(
    appsKey,
    () => deviceId
      ? getDeviceTopApps(deviceId, rangeArgDays, rangeArg)
      : getTopApps(rangeArgDays, APP_SEARCH_LIMIT, rangeArg),
  );

  // isValidating is true during background revalidation (stale data is still
  // shown, per the global keepPreviousData config) — a filter/date-range/
  // device change re-keys both SWR calls above, but without this the old
  // numbers just sit there with no indication a new fetch is in flight until
  // they silently swap. Same "Updating…" convention as Organization Overview.
  const isRefreshing = (summaryValidating && !!summary) || (appsValidating && !!apps);

  const filteredApps = useMemo(() => {
    if (!apps) return [];
    return smartSort(debouncedAppSearch, apps, a => [a.app_name]);
  }, [apps, debouncedAppSearch]);

  const totalActive = summary?.reduce((sum, d) => sum + d.active_seconds, 0) ?? 0;

  if (loading) return <PageLoader />;
  if (!can("reports", "view")) return <AccessDenied label="Reports" />;

  // Cold load only — keepPreviousData means these go false again the instant
  // ANY data exists, even for a different filter, so this never re-fires on a
  // filter change (isRefreshing above covers that case instead). Deliberately
  // NOT an early return: the header, date picker, and both search bars render
  // immediately either way, so the page is interactive before the first
  // response ever lands instead of being replaced by a full-page placeholder.
  const summaryColdLoading = summaryLoading && !summary;
  const appsColdLoading = appsLoading && !apps;

  return (
    /* Outer: fills the flex-col main, no overflow — all scroll happens inside */
    <div className="flex-1 min-h-0 flex flex-col gap-3">

      {/* Exports exactly what the filters above show. The dialog gets a copy of
          the range, so changing it there never touches the page filter. */}
      {exportOpen && can("reports", "view") && (
        <ExportReportDialog
          initialRange={effectiveRange}
          target={selectedDevice
            ? { kind: "device", deviceId: selectedDevice.id, hostname: selectedDevice.hostname, employeeName: selectedDevice.employee_name }
            : { kind: "fleet" }}
          onClose={() => setExportOpen(false)}
        />
      )}

      {/* ── Header + filters — fixed height, never scrolls away ── */}
      <div className="shrink-0 flex flex-col gap-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl sm:text-2xl font-semibold text-gray-900 leading-tight">Reports</h1>
              {isRefreshing && (
                <span className="text-xs text-gray-400 animate-pulse">Updating…</span>
              )}
            </div>
            <p className="text-gray-600 text-xs sm:text-sm mt-0.5">
              {selectedDevice
                ? `${selectedDevice.hostname}${selectedDevice.employee_name ? ` · ${selectedDevice.employee_name}` : ""}`
                : "Company-wide"}
              {" · "}{describeRange(effectiveRange)}
            </p>
          </div>

          {/* Date range picker — kept up here, next to the title, so the primary
              talent search below reads as the page's main filter. Supports real
              calendar ranges (This/Last Month, This/Last Year, Custom), not just
              a rolling N-days-ending-today window. */}
          <div className="flex flex-wrap items-center gap-2">
            <DateRangePicker value={dateRange} onChange={setDateRange} />
            {can("reports", "view") && (
              <button
                type="button"
                onClick={() => setExportOpen(true)}
                // A device filter whose device isn't resolved yet must not fall
                // back to a fleet export.
                disabled={!!deviceId && !selectedDevice}
                title="Export what these filters show"
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-brand-200 bg-brand-50 text-brand-700 hover:border-brand-300 hover:bg-brand-100 shadow-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Download size={14} />
                Export report
              </button>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-start gap-2 sm:gap-3">
          {/* Prominent talent search — find the right person even when several
              share a name, disambiguated by email (when permitted) and/or
              device hostname. */}
          <TalentSearchBar
            value={talentQuery}
            onChange={setTalentQuery}
            results={talentResults}
            onSelect={handleSelectTalent}
            placeholder="Find a talent by name or email…"
            label="Find a talent to filter reports"
            className="w-full sm:w-80"
          />

          {/* Device search — proper search bar, not a runtime-search dropdown.
              Lists every device individually (including unassigned ones), so
              picking a specific machine (e.g. for a talent with more than
              one) still works. */}
          <TalentSearchBar
            value={deviceQuery}
            onChange={v => { setDeviceQuery(v); if (!v) setDeviceId(""); }}
            results={deviceResults}
            onSelect={handleSelectDevice}
            placeholder="All users / devices — search hostname…"
            label="Find a device to filter reports"
            className="w-full sm:w-72"
          />
        </div>
      </div>

      {/* ── Stat cards — fixed height row, shrinks if no data ── */}
      {summaryColdLoading ? (
        <div className="shrink-0 grid grid-cols-2 sm:grid-cols-3 gap-3" aria-busy="true" aria-label="Loading stats">
          {[0, 1].map(i => (
            <div key={i} className="bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm">
              <div className="h-3 w-24 bg-gray-200 rounded animate-pulse mb-2" />
              <div className="h-6 w-20 bg-gray-200 rounded animate-pulse mb-2" />
              <div className="h-3 w-28 bg-gray-200 rounded animate-pulse" />
            </div>
          ))}
        </div>
      ) : totalActive > 0 && (
        <div className="shrink-0 grid grid-cols-2 sm:grid-cols-3 gap-3">
          <div className="bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm">
            <p className="text-xs text-gray-600 mb-0.5">Total active time</p>
            <p className="text-lg sm:text-xl font-semibold text-gray-900 leading-tight">{fmtSeconds(totalActive)}</p>
            <p className="text-xs text-gray-600 mt-0.5">over {rangeDays} {rangeDays === 1 ? "day" : "days"}</p>
          </div>
          {summary && summary.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm">
              <p className="text-xs text-gray-600 mb-0.5">Avg per day</p>
              <p className="text-lg sm:text-xl font-semibold text-gray-900 leading-tight">
                {fmtSeconds(Math.round(totalActive / summary.length))}
              </p>
              <p className="text-xs text-gray-600 mt-0.5">{summary.length} active days</p>
            </div>
          )}
        </div>
      )}

      {/* ── Chart — fixed height, never grows ── */}
      {summaryColdLoading ? (
        <div className="shrink-0 bg-white border border-gray-200 rounded-xl px-4 pt-4 pb-3 shadow-sm" aria-busy="true" aria-label="Loading chart">
          <div className="h-4 w-40 bg-gray-200 rounded animate-pulse mb-3" />
          {/* Matches DailyActiveBarChart's own h-32/h-40 footprint exactly, so
              nothing shifts when the real chart mounts in its place. */}
          <div className="h-32 sm:h-40 bg-gray-50 rounded animate-pulse" />
        </div>
      ) : summary && (
        <div className="shrink-0 bg-white border border-gray-200 rounded-xl px-4 pt-4 pb-3 shadow-sm">
          <p className="text-sm font-semibold text-gray-800 mb-2">Active time per day</p>
          {/* h-32 on mobile (128px), h-40 on sm+ (160px) */}
          <DailyActiveBarChart data={summary} />
        </div>
      )}

      {/* ── Top applications — fills ALL remaining vertical space, own scroller ── */}
      {appsColdLoading ? (
        <div className="flex-1 min-h-0 flex flex-col bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden" aria-busy="true" aria-label="Loading top applications">
          <div className="shrink-0 px-4 sm:px-5 pt-4 pb-3 border-b border-gray-100">
            <div className="h-4 w-32 bg-gray-200 rounded animate-pulse mb-3" />
            <div className="h-8 w-full bg-gray-100 rounded-lg animate-pulse" />
          </div>
          <div className="flex-1 min-h-0 overflow-hidden px-4 sm:px-5 py-3 space-y-3">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="flex items-center gap-2 sm:gap-3">
                <div className="h-3 w-5 bg-gray-200 rounded animate-pulse shrink-0" />
                <div className="h-3 w-28 sm:w-40 bg-gray-200 rounded animate-pulse shrink-0" />
                <div className="flex-1 h-2 bg-gray-100 rounded-full animate-pulse" />
                <div className="h-3 w-12 bg-gray-200 rounded animate-pulse shrink-0" />
              </div>
            ))}
          </div>
        </div>
      ) : apps && apps.length > 0 && (
        <div className="flex-1 min-h-0 flex flex-col bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">

          {/* Card header + search — sticky at top of card, never scrolls */}
          <div className="shrink-0 px-4 sm:px-5 pt-4 pb-3 border-b border-gray-100">
            <p className="text-sm font-semibold text-gray-800 mb-3">Top applications</p>
            <div className="relative">
              <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
              <input
                type="text"
                placeholder="Search applications…"
                value={appSearch}
                onChange={e => setAppSearch(e.target.value)}
                className="w-full pl-8 pr-8 py-1.5 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
              />
              {appSearch && (
                <button
                  onClick={() => setAppSearch("")}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600"
                >
                  <X size={13} />
                </button>
              )}
            </div>
          </div>

          {/* Scrollable rows — this is the ONLY element that scrolls */}
          <div className="flex-1 min-h-0 overflow-y-auto px-4 sm:px-5 py-3 space-y-2.5">
            {filteredApps.map((app, i) => {
              const pct = Math.round((app.seconds / (apps[0]?.seconds || 1)) * 100);
              return (
                <div key={app.app_name} className="flex items-center gap-2 sm:gap-3">
                  <span className="text-xs text-gray-600 w-5 text-right shrink-0">{i + 1}</span>
                  <span className="text-sm text-gray-700 w-28 sm:w-40 truncate shrink-0">{app.app_name}</span>
                  <div className="flex-1 min-w-0 bg-gray-100 rounded-full h-2">
                    <div
                      className="bg-brand-500 h-2 rounded-full transition-all"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-xs text-gray-600 w-14 sm:w-16 text-right shrink-0">
                    {fmtSeconds(app.seconds)}
                  </span>
                </div>
              );
            })}
            {filteredApps.length === 0 && (
              <p className="text-sm text-gray-600 py-6 text-center">
                {debouncedAppSearch.trim()
                  ? `No matches for "${debouncedAppSearch.trim()}" in the top ${apps.length} applications.`
                  : "No applications match your search."}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
