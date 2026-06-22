"use client";
import { Video } from "lucide-react";
import useSWR from "swr";
import { getMeetings, fmtSeconds } from "@/lib/api";
import type { MeetingsResponse } from "@/types";

function fmtClock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDayShort(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " ";
}

/**
 * Meetings section for the per-device and per-user detail views.
 * Pass exactly one of deviceId / employeeId. Self-fetching so pages add one line.
 */
export default function MeetingsCard({
  deviceId,
  employeeId,
  days = 7,
}: {
  deviceId?: string;
  employeeId?: string;
  days?: number;
}) {
  const key = deviceId ? `mtg-dev-${deviceId}` : `mtg-emp-${employeeId}`;
  const { data, isLoading } = useSWR<MeetingsResponse>(
    [key, days],
    () => getMeetings({ deviceId, employeeId, days }),
    { refreshInterval: 60_000 },
  );

  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-800 flex items-center gap-1.5">
          <Video size={14} className="text-brand-500" /> Meetings ({days}d)
        </h2>
        {data && (
          <span className="text-xs text-gray-600 tabular-nums">
            {data.count} {data.count === 1 ? "meeting" : "meetings"} · {fmtSeconds(data.total_active_secs)}
          </span>
        )}
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500 py-6 text-center">Loading meetings…</p>
      ) : !data || data.sessions.length === 0 ? (
        <p className="text-sm text-gray-600 py-6 text-center">No meetings recorded.</p>
      ) : (
        <div className="space-y-2.5 max-h-80 overflow-y-auto pr-1">
          {data.sessions.map((s, i) => (
            <div key={`${s.start_ts}-${i}`} className="border border-gray-100 rounded-lg px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold text-gray-800 flex items-center gap-1.5 min-w-0">
                  <span className="truncate">{s.platform}</span>
                  {s.ongoing && (
                    <span className="text-[10px] font-medium text-green-700 bg-green-50 px-1.5 py-0.5 rounded-full shrink-0">
                      live
                    </span>
                  )}
                </span>
                <span className="text-xs font-semibold text-brand-600 tabular-nums shrink-0">
                  {fmtSeconds(s.active_secs)}
                </span>
              </div>
              {s.title && <p className="text-xs text-gray-600 truncate mt-0.5">{s.title}</p>}
              <div className="flex items-center gap-2 mt-1 text-[11px] text-gray-500 min-w-0">
                <span className="shrink-0 tabular-nums">
                  {fmtDayShort(s.start_ts)}{fmtClock(s.start_ts)}–{s.ongoing ? "now" : fmtClock(s.end_ts)}
                </span>
                {s.apps.length > 0 && (
                  <span className="truncate">· {s.apps.slice(0, 3).map(a => a.app_name).join(", ")}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
