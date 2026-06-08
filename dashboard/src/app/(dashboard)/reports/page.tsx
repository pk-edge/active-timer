"use client";
import { useState, useMemo } from "react";
import useSWR from "swr";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import {
  getCompanySummary, getTopApps, getDevices,
  getDeviceSummary, getDeviceTopApps, fmtSeconds,
} from "@/lib/api";
import { useAuth } from "@/hooks/useAuth";
import { Search, X, ChevronDown } from "lucide-react";
import type { CompanyDay, AppUsage, Device } from "@/types";

const PERIODS = [
  { label: "7 days", value: 7 },
  { label: "14 days", value: 14 },
  { label: "30 days", value: 30 },
];

function SectionCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 mb-6 shadow-sm">
      <p className="text-sm font-semibold text-gray-800 mb-4">{title}</p>
      {children}
    </div>
  );
}

export default function ReportsPage() {
  useAuth();
  const [days, setDays]         = useState(7);
  const [deviceId, setDeviceId] = useState("");
  const [appSearch, setAppSearch] = useState("");

  const { data: devices } = useSWR<Device[]>("devices", getDevices);

  const selectedDevice = devices?.find(d => d.id === deviceId);

  const summaryKey = deviceId ? ["device-summary", deviceId, days] : ["summary", days];
  const appsKey    = deviceId ? ["device-apps", deviceId, days]    : ["top-apps", days];

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

  return (
    <div>
      {/* Header + filters */}
      <div className="flex flex-wrap items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900 mb-1">Reports</h1>
          <p className="text-gray-500 text-sm">
            {selectedDevice
              ? `${selectedDevice.hostname}${selectedDevice.employee_name ? ` · ${selectedDevice.employee_name}` : ""}`
              : "Company-wide"}
            {" · "}last {days} days
          </p>
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          {/* Device / User filter */}
          <div className="relative">
            <select
              value={deviceId}
              onChange={e => setDeviceId(e.target.value)}
              className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 cursor-pointer"
            >
              <option value="">All users / devices</option>
              {devices?.map(d => (
                <option key={d.id} value={d.id}>
                  {d.employee_name ? `${d.employee_name} (${d.hostname})` : d.hostname}
                </option>
              ))}
            </select>
            <ChevronDown size={14} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
          </div>

          {/* Period filter */}
          <div className="flex items-center bg-white border border-gray-300 rounded-lg overflow-hidden">
            {PERIODS.map(p => (
              <button
                key={p.value}
                onClick={() => setDays(p.value)}
                className={`px-3 py-2 text-sm transition ${
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

      {/* Summary stat */}
      {totalActive > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 mb-6">
          <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
            <p className="text-xs text-gray-500 mb-1">Total active time</p>
            <p className="text-xl sm:text-2xl font-semibold text-gray-900">{fmtSeconds(totalActive)}</p>
            <p className="text-xs text-gray-400 mt-0.5">over {days} days</p>
          </div>
          {summary && summary.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
              <p className="text-xs text-gray-500 mb-1">Avg per day</p>
              <p className="text-xl sm:text-2xl font-semibold text-gray-900">
                {fmtSeconds(Math.round(totalActive / summary.length))}
              </p>
              <p className="text-xs text-gray-400 mt-0.5">{summary.length} active days</p>
            </div>
          )}
        </div>
      )}

      {/* Daily active time chart */}
      {summary && (
        <SectionCard title="Active time per day">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={[...summary].reverse()} barCategoryGap="35%">
              <XAxis
                dataKey="date"
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tickFormatter={v => `${Math.floor(v / 3600)}h`}
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                width={28}
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
        </SectionCard>
      )}

      {/* Top applications */}
      {apps && apps.length > 0 && (
        <SectionCard title="Top applications">
          {/* App search */}
          <div className="relative mb-4">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
            <input
              type="text"
              placeholder="Search applications…"
              value={appSearch}
              onChange={e => setAppSearch(e.target.value)}
              className="w-full pl-9 pr-9 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
            />
            {appSearch && (
              <button
                onClick={() => setAppSearch("")}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
              >
                <X size={13} />
              </button>
            )}
          </div>

          <div className="space-y-2.5">
            {filteredApps.slice(0, 15).map((app, i) => {
              const pct = Math.round((app.seconds / (apps[0]?.seconds || 1)) * 100);
              return (
                <div key={app.app_name} className="flex items-center gap-3">
                  <span className="text-xs text-gray-400 w-5 text-right shrink-0">{i + 1}</span>
                  <span className="text-sm text-gray-700 w-36 truncate shrink-0">{app.app_name}</span>
                  <div className="flex-1 bg-gray-100 rounded-full h-2">
                    <div className="bg-brand-500 h-2 rounded-full transition-all" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="text-xs text-gray-500 w-16 text-right shrink-0">{fmtSeconds(app.seconds)}</span>
                </div>
              );
            })}
            {filteredApps.length === 0 && (
              <p className="text-sm text-gray-400 py-4 text-center">No applications match your search.</p>
            )}
          </div>
        </SectionCard>
      )}
    </div>
  );
}
