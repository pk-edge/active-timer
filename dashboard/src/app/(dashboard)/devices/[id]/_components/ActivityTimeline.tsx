"use client";
import { Monitor, Globe, Lock, MousePointer, Video, ShieldAlert } from "lucide-react";
import { fmtTime, fmtSeconds } from "@/lib/api";
import type { TimelineEvent } from "@/types";

const ICONS: Record<string, React.ReactNode> = {
  window_change: <Monitor size={12} />,
  url_visit:     <Globe size={12} />,
  url_blocked:   <ShieldAlert size={12} />,
  lock:          <Lock size={12} />,
  unlock:        <Lock size={12} />,
  idle_start:    <MousePointer size={12} />,
  idle_end:      <MousePointer size={12} />,
  meeting_start: <Video size={12} />,
  meeting_end:   <Video size={12} />,
};

const DOT_COLOR: Record<string, string> = {
  window_change: "bg-brand-500",
  url_visit:     "bg-purple-500",
  url_blocked:   "bg-red-500",
  lock:          "bg-red-400",
  unlock:        "bg-green-500",
  idle_start:    "bg-gray-300",
  idle_end:      "bg-gray-400",
  meeting_start: "bg-blue-500",
  meeting_end:   "bg-blue-400",
};

const ICON_COLOR: Record<string, string> = {
  window_change: "text-brand-500",
  url_visit:     "text-purple-500",
  url_blocked:   "text-red-500",
  lock:          "text-red-500",
  unlock:        "text-green-500",
  idle_start:    "text-gray-400",
  idle_end:      "text-gray-400",
  meeting_start: "text-blue-500",
  meeting_end:   "text-blue-400",
};

const BADGE_COLOR: Record<string, string> = {
  window_change: "bg-brand-50 text-brand-700",
  url_visit:     "bg-purple-50 text-purple-700",
  url_blocked:   "bg-red-50 text-red-600",
  lock:          "bg-red-50 text-red-600",
  unlock:        "bg-green-50 text-green-700",
  idle_start:    "bg-gray-100 text-gray-500",
  idle_end:      "bg-gray-100 text-gray-500",
  meeting_start: "bg-blue-50 text-blue-700",
  meeting_end:   "bg-blue-50 text-blue-600",
};

function EventRow({ e, isLast }: { e: TimelineEvent; isLast: boolean }) {
  const dot   = DOT_COLOR[e.event_type]  ?? "bg-gray-300";
  const icon  = ICON_COLOR[e.event_type] ?? "text-gray-400";
  const badge = BADGE_COLOR[e.event_type] ?? "bg-gray-100 text-gray-600";

  return (
    <div className="flex gap-0 group">
      {/* ── Prominent time column ── */}
      <div className="w-24 shrink-0 pt-2.5 pr-3 text-right">
        <span className="text-sm font-bold text-brand-700 font-mono tracking-tight leading-none">
          {fmtTime(e.timestamp_unix)}
        </span>
      </div>

      {/* ── Timeline spine ── */}
      <div className="flex flex-col items-center w-5 shrink-0">
        <div className={`w-2.5 h-2.5 rounded-full mt-3 shrink-0 ring-2 ring-white shadow-sm ${dot}`} />
        {!isLast && <div className="w-px flex-1 bg-gray-200 mt-1" />}
      </div>

      {/* ── Event content ── */}
      <div className={`flex-1 min-w-0 pl-3 pb-4 ${isLast ? "" : ""}`}>
        <div className="flex items-start justify-between gap-2 group-hover:bg-gray-50 rounded-lg px-2 py-2 -ml-2 transition">
          <div className="min-w-0 flex-1">
            {/* App name + event type badge */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className={`shrink-0 ${icon}`}>
                {ICONS[e.event_type] ?? <Monitor size={12} />}
              </span>
              {e.app_name ? (
                <span className="text-sm font-semibold text-gray-900 truncate">
                  {e.app_name}
                </span>
              ) : (
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${badge}`}>
                  {e.event_type === "lock"          ? "Screen locked"      :
                   e.event_type === "unlock"        ? "Screen unlocked"    :
                   e.event_type === "idle_start"    ? "Went idle"          :
                   e.event_type === "idle_end"      ? "Returned from idle" :
                   e.event_type === "meeting_start" ? "Meeting started"    :
                   e.event_type === "meeting_end"   ? "Meeting ended"      :
                   e.event_type === "url_blocked"   ? "Blocked URL"        :
                   e.event_type}
                </span>
              )}
              {e.app_name && (e.event_type === "meeting_start" || e.event_type === "meeting_end") && (
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full shrink-0 ${badge}`}>
                  {e.event_type === "meeting_start" ? "Meeting started" : "Meeting ended"}
                </span>
              )}
              {e.app_name && e.event_type === "url_blocked" && (
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full shrink-0 ${badge}`}>
                  Blocked URL
                </span>
              )}
              {e.duration > 0 && (
                <span className="text-xs font-medium bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full shrink-0">
                  {fmtSeconds(e.duration)}
                </span>
              )}
            </div>

            {/* Window title */}
            {e.window_title && (
              <p className="text-xs text-gray-400 mt-0.5 truncate pl-5" title={e.window_title}>
                {e.window_title}
              </p>
            )}

            {/* URL */}
            {e.url && (
              <p className="text-xs text-purple-500 mt-0.5 truncate pl-5" title={e.url}>
                {e.url}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

interface Props { events: TimelineEvent[] | undefined }

export default function ActivityTimeline({ events }: Props) {
  if (!events) return null;
  if (events.length === 0) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl py-16 text-center shadow-sm">
        <Monitor size={36} className="mx-auto mb-3 text-gray-200" />
        <p className="text-gray-400 text-sm font-medium">No activity recorded for this date.</p>
        <p className="text-gray-300 text-xs mt-1">Try selecting a different date</p>
      </div>
    );
  }

  return (
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-100 bg-gray-50 flex items-center justify-between">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Activity Feed</p>
        <span className="text-xs font-medium bg-brand-50 text-brand-600 px-2 py-0.5 rounded-full">
          {events.length} events
        </span>
      </div>

      {/* Timeline */}
      <div className="px-4 py-3">
        {[...events].reverse().map((e, i) => (
          <EventRow key={e.id} e={e} isLast={i === events.length - 1} />
        ))}
      </div>
    </div>
  );
}
