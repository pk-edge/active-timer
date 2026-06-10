"use client";
import { useState, useMemo } from "react";
import Link from "next/link";
import useSWR from "swr";
import { format } from "date-fns";
import {
  Monitor, Globe, Lock, MousePointer,
  Search, X, ChevronDown } from "lucide-react";
import { getLogs, getDevices, fmtTime, fmtSeconds } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { LogEvent, Device } from "@/types";
import RefreshButton from "@/components/RefreshButton";

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
  idle_start:    "bg-gray-100 text-gray-500 border-gray-200",
  idle_end:      "bg-gray-50 text-gray-500 border-gray-200" };

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

function EventBadge({ type }: { type: string }) {
  const cls = EVENT_COLOR[type] ?? "bg-gray-100 text-gray-500 border-gray-200";
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
      <td className="py-3 px-4 text-xs text-gray-500 whitespace-nowrap font-mono">
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
            <span className="text-xs text-gray-400 truncate" title={e.window_title}>
              {e.window_title}
            </span>
          )}
          {e.url && (
            <span className="text-xs text-purple-500 truncate" title={e.url}>
              {e.url}
            </span>
          )}
          {!e.app_name && !e.window_title && !e.url && (
            <span className="text-xs text-gray-400 italic">{e.event_type}</span>
          )}
        </div>
      </td>
      <td className="py-3 px-4 text-xs text-gray-500 whitespace-nowrap">
        {e.duration > 0
          ? <span className="bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full font-medium">{fmtSeconds(e.duration)}</span>
          : <span className="text-gray-300">—</span>}
      </td>
      <td className="py-3 px-4 text-xs whitespace-nowrap">
        <Link
          href={`/devices/${e.device_id}`}
          className="text-gray-500 hover:text-brand-600 transition font-medium"
        >
          {e.hostname}
        </Link>
      </td>
    </tr>
  );
}

export default function LogsPage() {
  const { loading, can } = useUser();
  const today = format(new Date(), "yyyy-MM-dd");
  const [selDate,   setSelDate]   = useState(today);
  const [selDevice, setSelDevice] = useState("");
  const [selType,   setSelType]   = useState("");
  const [search,    setSearch]    = useState("");

  const { data: devices } = useSWR<Device[]>("devices", getDevices);

  const logsOpts = (() => {
    const opts: { since?: number; until?: number; device_id?: string; event_type?: string } = {};
    if (selDate) {
      opts.since = Math.floor(new Date(selDate + "T00:00:00").getTime() / 1000);
      opts.until = opts.since + 86400;
    }
    if (selDevice) opts.device_id  = selDevice;
    if (selType)   opts.event_type = selType;
    return opts;
  })();

  const { data: logs, isLoading, mutate } = useSWR<LogEvent[]>(
    ["logs", selDate, selDevice, selType],
    () => getLogs(logsOpts),
    { refreshInterval: 15_000 },
  );

  const filtered = useMemo(() => {
    if (!logs) return [];
    if (!search.trim()) return logs;
    const q = search.toLowerCase();
    return logs.filter(
      e =>
        (e.app_name     ?? "").toLowerCase().includes(q) ||
        (e.window_title ?? "").toLowerCase().includes(q) ||
        (e.url          ?? "").toLowerCase().includes(q) ||
        e.hostname.toLowerCase().includes(q),
    );
  }, [logs, search]);

  const counts = logs?.reduce<Record<string, number>>((acc, e) => {
    acc[e.event_type] = (acc[e.event_type] ?? 0) + 1;
    return acc;
  }, {});

  if (loading) return null;
  if (!can("logs", "view")) return <AccessDenied label="Activity Logs" />;

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6 shrink-0">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Activity Logs</h1>
          <p className="text-gray-500 text-sm mt-0.5">Live feed — refreshes every 15 s</p>
        </div>
        <RefreshButton onRefresh={() => mutate()} />
      </div>

      {/* Filters row */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 mb-5 shadow-sm shrink-0">
        <div className="flex flex-wrap gap-3 items-center">
          {/* Date */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500 font-medium">Date</label>
            <input
              type="date"
              value={selDate}
              max={today}
              onChange={e => setSelDate(e.target.value)}
              className="bg-white border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
            />
          </div>

          {/* Device */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500 font-medium">Device</label>
            <div className="relative">
              <select
                value={selDevice}
                onChange={e => setSelDevice(e.target.value)}
                className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
              >
                <option value="">All devices</option>
                {devices?.map(d => (
                  <option key={d.id} value={d.id}>
                    {d.employee_name ? `${d.employee_name} — ${d.hostname}` : d.hostname}
                  </option>
                ))}
              </select>
              <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
            </div>
          </div>

          {/* Event type */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500 font-medium">Event type</label>
            <div className="relative">
              <select
                value={selType}
                onChange={e => setSelType(e.target.value)}
                className="appearance-none bg-white border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
              >
                <option value="">All types</option>
                {EVENT_TYPES.map(t => (
                  <option key={t} value={t}>{EVENT_LABEL[t] ?? t}</option>
                ))}
              </select>
              <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
            </div>
          </div>

          {/* Smart search */}
          <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
            <label className="text-xs text-gray-500 font-medium">Search</label>
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              <input
                type="text"
                placeholder="App, window title, URL, hostname…"
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full pl-9 pr-8 py-2 bg-white border border-gray-300 rounded-lg text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
              />
              {search && (
                <button
                  onClick={() => setSearch("")}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                >
                  <X size={13} />
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Event count summary chips */}
        {counts && Object.keys(counts).length > 0 && (
          <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t border-gray-100">
            {Object.entries(counts)
              .sort((a, b) => b[1] - a[1])
              .map(([type, count]) => (
                <button
                  key={type}
                  onClick={() => setSelType(selType === type ? "" : type)}
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
            <span className="text-xs text-gray-400 ml-auto self-center">
              {filtered.length}{search ? ` of ${logs?.length}` : ""} event{filtered.length !== 1 ? "s" : ""}
            </span>
          </div>
        )}
      </div>

      {/* Table — fills remaining height and scrolls internally */}
      <div className="flex-1 min-h-0 bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm flex flex-col">
        {isLoading ? (
          <div className="py-16 text-center text-gray-400 text-sm animate-pulse">Loading activity…</div>
        ) : !filtered.length ? (
          <div className="py-16 text-center text-gray-400 text-sm">
            <Search size={32} className="mx-auto mb-3 text-gray-200" />
            {search ? "No events match your search." : "No activity found for the selected filters."}
          </div>
        ) : (
          <>
            {/* Sticky header */}
            <div className="shrink-0 overflow-x-auto border-b border-gray-200">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50">
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">Time</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">Type</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Activity</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">Duration</th>
                    <th className="py-3 px-4 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">Device</th>
                  </tr>
                </thead>
              </table>
            </div>
            {/* Scrollable body */}
            <div className="flex-1 overflow-y-auto overflow-x-auto min-h-0">
              <table className="w-full text-sm">
                <tbody>
                  {filtered.map(e => <LogRow key={e.id} e={e} />)}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
