"use client";
import PageLoader from "@/components/PageLoader";
import PageSkeleton from "@/components/PageSkeleton";
import { useState, useMemo } from "react";
import dynamic from "next/dynamic";
import useSWR from "swr";
import {
  getCompanySummary, getTopApps, getDevices, getUsers,
  getDeviceSummary, getDeviceTopApps, fmtSeconds,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { smartSort } from "@/lib/smartSearch";
import TalentSearchBar, { type TalentSearchResult } from "@/components/TalentSearchBar";
import { AccessDenied } from "@/components/AccessDenied";

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
import { Search, X, ChevronDown } from "lucide-react";
import type { CompanyDay, AppUsage, Device, User } from "@/types";

const PERIODS = [
  { label: "7 days",  value: 7  },
  { label: "14 days", value: 14 },
  { label: "30 days", value: 30 },
];

// Company-wide top-apps caps at 100 server-side (backend/app/api/v1/endpoints/
// reports.py). Ask for the max so the in-card search box has more than the
// top 20 to search over — otherwise anything ranked below #20 is invisible
// to the search box and looks like a false "no match".
const APP_SEARCH_LIMIT = 100;

export default function ReportsPage() {
  const { loading, can } = useUser();
  const [days, setDays]         = useState(7);
  const [deviceId, setDeviceId] = useState("");
  const [appSearch, setAppSearch] = useState("");
  const [talentQuery, setTalentQuery] = useState("");
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
      // A person with multiple devices lands on their first one; the
      // existing `<select>` below still lists every device individually, so
      // switching to a specific one of their other machines stays possible.
      setDeviceId(person.deviceIds[0]);
    }
    setTalentQuery("");
  }

  const summaryKey = deviceId ? ["device-summary", deviceId, days] : ["summary", days];
  const appsKey    = deviceId ? ["device-apps",   deviceId, days]  : ["top-apps", days];

  const { data: summary, isLoading: summaryLoading } = useSWR<CompanyDay[]>(
    summaryKey,
    () => deviceId ? getDeviceSummary(deviceId, days) : getCompanySummary(days),
  );
  const { data: apps } = useSWR<AppUsage[]>(
    appsKey,
    () => deviceId ? getDeviceTopApps(deviceId, days) : getTopApps(days, APP_SEARCH_LIMIT),
  );

  const filteredApps = useMemo(() => {
    if (!apps) return [];
    return smartSort(debouncedAppSearch, apps, a => [a.app_name]);
  }, [apps, debouncedAppSearch]);

  const totalActive = summary?.reduce((sum, d) => sum + d.active_seconds, 0) ?? 0;

  if (loading) return <PageLoader />;
  if (!can("reports", "view")) return <AccessDenied label="Reports" />;
  if (summaryLoading && !summary) return <PageSkeleton variant="dashboard" cards={2} />;

  return (
    /* Outer: fills the flex-col main, no overflow — all scroll happens inside */
    <div className="flex-1 min-h-0 flex flex-col gap-3">

      {/* ── Header + filters — fixed height, never scrolls away ── */}
      <div className="shrink-0 flex flex-col gap-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl sm:text-2xl font-semibold text-gray-900 leading-tight">Reports</h1>
            <p className="text-gray-600 text-xs sm:text-sm mt-0.5">
              {selectedDevice
                ? `${selectedDevice.hostname}${selectedDevice.employee_name ? ` · ${selectedDevice.employee_name}` : ""}`
                : "Company-wide"}
              {" · "}last {days} days
            </p>
          </div>

          {/* Period toggle — kept up here, next to the title, so the primary
              talent search below reads as the page's main filter. */}
          <div className="flex items-center bg-white border border-gray-300 rounded-lg overflow-hidden">
            {PERIODS.map(p => (
              <button
                key={p.value}
                onClick={() => setDays(p.value)}
                className={`px-2.5 sm:px-3 py-1.5 sm:py-2 text-xs sm:text-sm transition ${
                  days === p.value
                    ? "bg-brand-600 text-white font-medium"
                    : "text-gray-600 hover:bg-gray-50"
                }`}
              >
                {p.label}
              </button>
            ))}
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

          {/* Device / User filter — preserved as-is, including "All users /
              devices" and every individual device, so picking a specific
              device (e.g. for a talent with more than one) still works. */}
          <div className="relative">
            <select
              value={deviceId}
              onChange={e => setDeviceId(e.target.value)}
              className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-8 py-1.5 sm:py-2 text-xs sm:text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 cursor-pointer"
            >
              <option value="">All users / devices</option>
              {devices?.map(d => (
                <option key={d.id} value={d.id}>
                  {d.employee_name ? `${d.employee_name} (${d.hostname})` : d.hostname}
                </option>
              ))}
            </select>
            <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
          </div>
        </div>
      </div>

      {/* ── Stat cards — fixed height row, shrinks if no data ── */}
      {totalActive > 0 && (
        <div className="shrink-0 grid grid-cols-2 sm:grid-cols-3 gap-3">
          <div className="bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm">
            <p className="text-xs text-gray-600 mb-0.5">Total active time</p>
            <p className="text-lg sm:text-xl font-semibold text-gray-900 leading-tight">{fmtSeconds(totalActive)}</p>
            <p className="text-xs text-gray-600 mt-0.5">over {days} days</p>
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
      {summary && (
        <div className="shrink-0 bg-white border border-gray-200 rounded-xl px-4 pt-4 pb-3 shadow-sm">
          <p className="text-sm font-semibold text-gray-800 mb-2">Active time per day</p>
          {/* h-32 on mobile (128px), h-40 on sm+ (160px) */}
          <DailyActiveBarChart data={summary} />
        </div>
      )}

      {/* ── Top applications — fills ALL remaining vertical space, own scroller ── */}
      {apps && apps.length > 0 && (
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
