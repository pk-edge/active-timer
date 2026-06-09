"use client";
import { use, useState, useMemo } from "react";
import Link from "next/link";
import useSWR from "swr";
import { format } from "date-fns";
import { ArrowLeft, Laptop, User } from "lucide-react";
import { getTimeline, getDailyStats, getDevices, fmtSeconds } from "@/lib/api";
import { useAuth } from "@/hooks/useAuth";
import DailyChart       from "./_components/DailyChart";
import ActivityTimeline from "./_components/ActivityTimeline";
import InsightsPanel    from "./_components/InsightsPanel";
import type { TimelineEvent, DailyStat, Device } from "@/types";

export default function DevicePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  useAuth();
  const today = format(new Date(), "yyyy-MM-dd");
  const [selDate, setSelDate] = useState(today);

  const timelineSince = selDate
    ? Math.floor(new Date(selDate + "T00:00:00").getTime() / 1000)
    : undefined;
  const timelineUntil = timelineSince ? timelineSince + 86400 : undefined;

  const { data: events } = useSWR<TimelineEvent[]>(
    ["timeline", id, selDate],
    () => getTimeline(id, timelineSince, timelineUntil),
    { refreshInterval: 30_000 },
  );

  const { data: stats } = useSWR<DailyStat[]>(
    ["stats", id],
    () => getDailyStats(id, 7),
  );

  // Fetch device info for hostname / employee name
  const { data: devices } = useSWR<Device[]>("devices", getDevices);
  const device = devices?.find(d => d.id === id);

  // Compute active time directly from events (avoids UTC/local date mismatch in stats)
  const activeSecsToday = useMemo(() => {
    if (!events) return 0;
    return events
      .filter(e => e.event_type === "window_change" && e.duration > 0)
      .reduce((sum, e) => sum + e.duration, 0);
  }, [events]);

  const idleSecsToday = useMemo(() => {
    if (!events) return 0;
    return events
      .filter(e => e.event_type === "idle_start" && e.duration > 0)
      .reduce((sum, e) => sum + e.duration, 0);
  }, [events]);

  return (
    <div className="flex flex-col h-full min-h-0">

      {/* ── Back link ── */}
      <Link
        href="/"
        className="inline-flex items-center gap-1.5 text-gray-400 text-sm mb-3 hover:text-gray-700 transition w-fit"
      >
        <ArrowLeft size={13} /> All devices
      </Link>

      {/* ── Page header ── */}
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl font-bold text-gray-900 flex items-center gap-2">
            <Laptop size={20} className="text-brand-500 shrink-0" />
            {device ? device.hostname : `Device — ${id.slice(0, 8)}`}
          </h1>
          <div className="flex items-center gap-3 mt-1 flex-wrap">
            {device?.employee_name && (
              <span className="flex items-center gap-1 text-xs text-gray-500">
                <User size={11} />
                {device.employee_name}
              </span>
            )}
            {device?.platform && (
              <span className="text-xs text-gray-400 capitalize bg-gray-100 px-2 py-0.5 rounded-full">
                {device.platform}
              </span>
            )}
            <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
              device?.is_online
                ? "bg-green-50 text-green-600"
                : "bg-gray-100 text-gray-500"
            }`}>
              {device?.is_online ? "● Online" : "○ Offline"}
            </span>
          </div>
          <p className="text-sm mt-1.5">
            <span className="text-gray-500">Active on {selDate}:</span>{" "}
            <span className="text-green-600 font-bold">{fmtSeconds(activeSecsToday)}</span>
            {idleSecsToday > 0 && (
              <span className="text-gray-400 text-xs ml-2">· {fmtSeconds(idleSecsToday)} idle</span>
            )}
          </p>
        </div>

        <div className="flex flex-col gap-1 items-end shrink-0">
          <label className="text-xs text-gray-400 font-medium">Date</label>
          <input
            type="date"
            value={selDate}
            max={today}
            onChange={e => setSelDate(e.target.value)}
            className="bg-white border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
          />
        </div>
      </div>

      {/* ── Two-column layout — stacks on mobile, side-by-side on desktop ── */}
      <div className="flex flex-col lg:flex-row gap-4 lg:gap-5 flex-1 min-h-0 overflow-y-auto lg:overflow-hidden">

        {/* Insights + chart — first on mobile, right column on desktop */}
        <div className="w-full lg:w-72 lg:order-2 shrink-0 flex flex-col gap-3 lg:overflow-y-auto">
          {stats && <DailyChart stats={stats} selDate={selDate} />}
          <InsightsPanel
            events={events}
            activeSecsToday={activeSecsToday}
            idleSecsToday={idleSecsToday}
          />
        </div>

        {/* Activity feed — below on mobile, left column on desktop */}
        <div className="flex-1 min-w-0 lg:order-1 lg:overflow-y-auto">
          <ActivityTimeline events={events} />
        </div>

      </div>
    </div>
  );
}
