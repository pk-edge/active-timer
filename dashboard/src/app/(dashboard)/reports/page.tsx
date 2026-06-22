"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useMemo } from "react";
import useSWR from "swr";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import {
  getCompanySummary, getTopApps, getDevices,
  getDeviceSummary, getDeviceTopApps, fmtSeconds,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import { Search, X, ChevronDown } from "lucide-react";
import type { CompanyDay, AppUsage, Device } from "@/types";

const PERIODS = [
  { label: "7 days",  value: 7  },
  { label: "14 days", value: 14 },
  { label: "30 days", value: 30 },
];

export default function ReportsPage() {
  const { loading, can } = useUser();
  const [days, setDays]         = useState(7);
  const [deviceId, setDeviceId] = useState("");
  const [appSearch, setAppSearch] = useState("");

  const { data: devices } = useSWR<Device[]>("devices", getDevices);
  const selectedDevice = devices?.find(d => d.id === deviceId);

  const summaryKey = deviceId ? ["device-summary", deviceId, days] : ["summary", days];
  const appsKey    = deviceId ? ["device-apps",   deviceId, days]  : ["top-apps", days];

  const { data: summary } = useSWR<CompanyDay[]>(
    summaryKey,
    () => deviceId ? getDeviceSummary(deviceId, days) : getCompanySummary(days),
  );
  const { data: apps } = useSWR<AppUsage[]>(
    appsKey,
    () => deviceId ? getDeviceTopApps(deviceId, days) : getTopApps(days),
  );

  const filteredApps = useMemo(() => {
    if (!apps) return [];
    if (!appSearch.trim()) return apps;
    const q = appSearch.toLowerCase();
    return apps.filter(a => a.app_name.toLowerCase().includes(q));
  }, [apps, appSearch]);

  const totalActive = summary?.reduce((sum, d) => sum + d.active_seconds, 0) ?? 0;

  if (loading) return <PageLoader />;
  if (!can("reports", "view")) return <AccessDenied label="Reports" />;

  return (
    /* Outer: fills the flex-col main, no overflow — all scroll happens inside */
    <div className="flex-1 min-h-0 flex flex-col gap-3">

      {/* ── Header + filters — fixed height, never scrolls away ── */}
      <div className="shrink-0 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900 leading-tight">Reports</h1>
          <p className="text-gray-600 text-xs sm:text-sm mt-0.5">
            {selectedDevice
              ? `${selectedDevice.hostname}${selectedDevice.employee_name ? ` · ${selectedDevice.employee_name}` : ""}`
              : "Company-wide"}
            {" · "}last {days} days
          </p>
        </div>

        <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
          {/* Device / User filter */}
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

          {/* Period toggle */}
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
          <div className="h-32 sm:h-40">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={[...summary].reverse()} barCategoryGap="35%">
                <XAxis
                  dataKey="date"
                  tick={{ fill: "#9ca3af", fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tickFormatter={v => `${Math.floor(v / 3600)}h`}
                  tick={{ fill: "#9ca3af", fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  width={26}
                />
                <Tooltip
                  formatter={(v: number) => [fmtSeconds(v), "Active"]}
                  contentStyle={{
                    background: "#fff",
                    border: "1px solid #e5e7eb",
                    borderRadius: 8,
                    fontSize: 12,
                    color: "#111827",
                  }}
                />
                <Bar dataKey="active_seconds" fill="#4a0f70" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
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
              <p className="text-sm text-gray-600 py-6 text-center">No applications match your search.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
