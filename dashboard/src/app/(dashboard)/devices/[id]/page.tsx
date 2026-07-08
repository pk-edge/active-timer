"use client";
import { use, useState, useMemo } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import useSWR from "swr";
import { format } from "date-fns";
import { ArrowLeft, Laptop, User } from "lucide-react";
import { getTimeline, getDailyStats, getDevices, setDeviceHeadless, fmtSeconds } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import PageLoader from "@/components/PageLoader";
import { AccessDenied } from "@/components/AccessDenied";
// Lazy: recharts (~100KB) loads only when this device-detail view renders, not
// as part of any shared bundle that lighter pages would otherwise pay for.
const DailyChart = dynamic(() => import("./_components/DailyChart"), {
  ssr: false,
  loading: () => (
    <div className="bg-white border border-gray-200 rounded-xl px-4 pt-3 pb-2 shadow-sm shrink-0">
      <div className="h-[110px] animate-pulse flex items-end gap-2 pt-4">
        {[60, 80, 45, 90, 70, 55, 75].map((p, i) => (
          <div key={i} className="flex-1 bg-gray-100 rounded-t" style={{ height: `${p}%` }} />
        ))}
      </div>
    </div>
  ),
});
import ActivityTimeline from "./_components/ActivityTimeline";
import InsightsPanel    from "./_components/InsightsPanel";
import MeetingsCard     from "@/components/MeetingsCard";
import type { TimelineEvent, DailyStat, Device } from "@/types";

export default function DevicePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { loading, can } = useUser();
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
  const { data: devices, mutate: mutateDevices } = useSWR<Device[]>("devices", getDevices);
  const device = devices?.find(d => d.id === id);

  async function changeHeadless(override: boolean | null) {
    await setDeviceHeadless(id, override);
    mutateDevices();
  }

  // OS lock/login screen apps — never count as active work time.
  // Must stay in sync with backend SYSTEM_IDLE_APPS and agent LOCK_SCREEN_APPS.
  const LOCK_SCREEN_APPS = new Set([
    "loginwindow", "ScreenSaverEngine",
    "System Idle Process", "LockApp", "LogonUI", "Idle", "screensaver",
  ]);

  // Compute active time directly from events (avoids UTC/local date mismatch in stats)
  const activeSecsToday = useMemo(() => {
    if (!events) return 0;
    return events
      .filter(e =>
        e.event_type === "window_change" &&
        e.duration > 0 &&
        e.app_name != null &&
        !LOCK_SCREEN_APPS.has(e.app_name)
      )
      .reduce((sum, e) => sum + e.duration, 0);
  }, [events]);

  const idleSecsToday = useMemo(() => {
    if (!events) return 0;
    return events
      .filter(e => e.event_type === "idle_start" && e.duration > 0)
      .reduce((sum, e) => sum + e.duration, 0);
  }, [events]);

  if (loading) return <PageLoader />;
  if (!can("devices", "view")) return <AccessDenied label="Devices" />;

  return (
    <div className="flex flex-col h-full min-h-0">

      {/* ── Back link ── */}
      <Link
        href="/devices"
        className="inline-flex items-center gap-1.5 text-gray-600 text-sm mb-3 hover:text-gray-700 transition w-fit"
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
              <span className="flex items-center gap-1 text-xs text-gray-600">
                <User size={11} />
                {device.employee_name}
              </span>
            )}
            {device?.platform && (
              <span className="text-xs text-gray-600 capitalize bg-gray-100 px-2 py-0.5 rounded-full">
                {device.platform}
              </span>
            )}
            <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
              device?.is_online
                ? "bg-green-50 text-green-600"
                : "bg-gray-100 text-gray-600"
            }`}>
              {device?.is_online ? "● Online" : "○ Offline"}
            </span>
          </div>
          <p className="text-sm mt-1.5">
            <span className="text-gray-600">Active on {selDate}:</span>{" "}
            <span className="text-green-600 font-bold">{fmtSeconds(activeSecsToday)}</span>
            {idleSecsToday > 0 && (
              <span className="text-gray-600 text-xs ml-2">· {fmtSeconds(idleSecsToday)} idle</span>
            )}
          </p>
        </div>

        <div className="flex flex-col gap-1 items-end shrink-0">
          <label className="text-xs text-gray-600 font-medium">Date</label>
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
          <MeetingsCard deviceId={id} days={7} />

          {/* ── Agent visibility (per-device override) ── */}
          <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-3">
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-2">
              Agent visibility
            </p>
            <div className="flex gap-1">
              {([
                { label: "Inherit", val: null },
                { label: "Visible", val: false },
                { label: "Hidden",  val: true },
              ] as { label: string; val: boolean | null }[]).map(opt => {
                const active = (device?.headless_override ?? null) === opt.val;
                return (
                  <button
                    key={opt.label}
                    onClick={() => changeHeadless(opt.val)}
                    className={`flex-1 text-xs py-1.5 rounded-lg border transition ${
                      active
                        ? "bg-brand-50 border-brand-300 text-brand-700 font-semibold"
                        : "border-gray-200 text-gray-600 hover:bg-gray-50"
                    }`}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
            <p className="text-[11px] text-gray-500 mt-2">
              {device?.headless ? "Hidden — no timer or tray icon." : "Visible — timer shown."}
              {(device?.headless_override ?? null) === null && " (inheriting company default)"}
            </p>
          </div>
        </div>

        {/* Activity feed — below on mobile, left column on desktop */}
        <div className="flex-1 min-w-0 lg:order-1 lg:overflow-y-auto">
          <ActivityTimeline events={events} />
        </div>

      </div>
    </div>
  );
}
