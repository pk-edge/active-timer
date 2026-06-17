"use client";
import { useMemo, useState } from "react";
import { ArrowLeftRight, Globe, Lock, TrendingUp, Clock, ChevronDown, ChevronUp } from "lucide-react";
import { fmtSeconds } from "@/lib/api";
import type { TimelineEvent } from "@/types";

interface Props {
  events:           TimelineEvent[] | undefined;
  activeSecsToday:  number;
  idleSecsToday:    number;
}

export default function InsightsPanel({ events, activeSecsToday, idleSecsToday }: Props) {
  const insights = useMemo(() => {
    if (!events || events.length === 0) return null;

    // OS lock/login screen apps — excluded from all active-time metrics.
    const LOCK_SCREEN_APPS = new Set([
      "loginwindow", "ScreenSaverEngine",
      "System Idle Process", "LockApp", "LogonUI", "Idle", "screensaver",
    ]);

    // Top apps by total duration
    const appMap: Record<string, number> = {};
    events.forEach(e => {
      if (e.event_type === "window_change" && e.app_name && !LOCK_SCREEN_APPS.has(e.app_name)) {
        appMap[e.app_name] = (appMap[e.app_name] ?? 0) + (e.duration || 0);
      }
    });
    const topApps = Object.entries(appMap).sort((a, b) => b[1] - a[1]);

    // Event type counts
    const typeCounts: Record<string, number> = {};
    events.forEach(e => { typeCounts[e.event_type] = (typeCounts[e.event_type] ?? 0) + 1; });

    // Peak hour
    const hourMap: Record<number, number> = {};
    events.forEach(e => {
      if (e.event_type === "window_change" && e.duration > 0) {
        const h = new Date(e.timestamp_unix * 1000).getHours();
        hourMap[h] = (hourMap[h] ?? 0) + e.duration;
      }
    });
    const peakEntry = Object.entries(hourMap).sort((a, b) => Number(b[1]) - Number(a[1]))[0];
    const peakHour  = peakEntry ? Number(peakEntry[0]) : null;

    // Top domain from url_visit
    const domainMap: Record<string, number> = {};
    events.forEach(e => {
      if (e.event_type === "url_visit" && e.url) {
        try {
          const host = new URL(e.url).hostname.replace(/^www\./, "");
          domainMap[host] = (domainMap[host] ?? 0) + 1;
        } catch { /* skip malformed URLs */ }
      }
    });
    const topDomain = Object.entries(domainMap).sort((a, b) => b[1] - a[1])[0];

    return {
      topApps,
      appSwitches: typeCounts["window_change"] ?? 0,
      urlVisits:   typeCounts["url_visit"]     ?? 0,
      lockCount:   typeCounts["lock"]          ?? 0,
      peakHour,
      topDomain,
    };
  }, [events]);

  const [showAllApps, setShowAllApps] = useState(false);

  const focusPct = activeSecsToday + idleSecsToday > 0
    ? Math.round((activeSecsToday / (activeSecsToday + idleSecsToday)) * 100)
    : 0;

  const maxAppSecs = insights?.topApps?.[0]?.[1] ?? 1;

  if (!insights) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-4 text-center shadow-sm">
        <p className="text-xs text-gray-400">No activity data for this date.</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">

      {/* ── Quick stats row ── */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-3">
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Usage Breakdown</p>
        <div className="grid grid-cols-2 gap-2">
          {/* Active */}
          <div className="bg-green-50 rounded-lg px-3 py-2">
            <p className="text-base font-bold text-green-600 leading-tight">{fmtSeconds(activeSecsToday)}</p>
            <p className="text-xs text-green-500 mt-0.5">Active</p>
          </div>
          {/* Idle */}
          <div className="bg-amber-50 rounded-lg px-3 py-2">
            <p className="text-base font-bold text-amber-500 leading-tight">{fmtSeconds(idleSecsToday)}</p>
            <p className="text-xs text-amber-400 mt-0.5">Idle</p>
          </div>
          {/* App switches */}
          <div className="bg-brand-50 rounded-lg px-3 py-2 flex items-center gap-2">
            <ArrowLeftRight size={13} className="text-brand-400 shrink-0" />
            <div>
              <p className="text-base font-bold text-brand-600 leading-tight">{insights.appSwitches}</p>
              <p className="text-xs text-brand-400">Switches</p>
            </div>
          </div>
          {/* URL visits */}
          <div className="bg-purple-50 rounded-lg px-3 py-2 flex items-center gap-2">
            <Globe size={13} className="text-purple-400 shrink-0" />
            <div>
              <p className="text-base font-bold text-purple-600 leading-tight">{insights.urlVisits}</p>
              <p className="text-xs text-purple-400">URLs</p>
            </div>
          </div>
        </div>

        {/* Focus ratio bar */}
        {activeSecsToday + idleSecsToday > 0 && (
          <div className="mt-3">
            <div className="flex justify-between text-xs text-gray-400 mb-1">
              <span className="flex items-center gap-1"><TrendingUp size={10} /> Focus ratio</span>
              <span className="font-semibold text-gray-700">{focusPct}%</span>
            </div>
            <div className="h-1.5 bg-amber-100 rounded-full overflow-hidden">
              <div className="h-full bg-green-400 rounded-full" style={{ width: `${focusPct}%` }} />
            </div>
          </div>
        )}
      </div>

      {/* ── Peak hour + top domain ── */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-3">
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Highlights</p>
        <div className="space-y-2">
          {insights.peakHour !== null && (
            <div className="flex items-center gap-2.5">
              <div className="w-6 h-6 rounded-md bg-brand-50 flex items-center justify-center shrink-0">
                <Clock size={12} className="text-brand-500" />
              </div>
              <div className="min-w-0">
                <p className="text-xs font-semibold text-gray-800">
                  {insights.peakHour % 12 || 12}:00 {insights.peakHour < 12 ? "AM" : "PM"}
                  {" – "}
                  {(insights.peakHour + 1) % 12 || 12}:00 {(insights.peakHour + 1) < 12 ? "AM" : "PM"}
                </p>
                <p className="text-xs text-gray-400">Peak hour</p>
              </div>
            </div>
          )}

          {insights.topDomain && (
            <div className="flex items-center gap-2.5">
              <div className="w-6 h-6 rounded-md bg-purple-50 flex items-center justify-center shrink-0">
                <Globe size={12} className="text-purple-500" />
              </div>
              <div className="min-w-0">
                <p className="text-xs font-semibold text-gray-800 truncate">{insights.topDomain[0]}</p>
                <p className="text-xs text-gray-400">Top site · {insights.topDomain[1]} visits</p>
              </div>
            </div>
          )}

          {insights.lockCount > 0 && (
            <div className="flex items-center gap-2.5">
              <div className="w-6 h-6 rounded-md bg-red-50 flex items-center justify-center shrink-0">
                <Lock size={12} className="text-red-400" />
              </div>
              <div>
                <p className="text-xs font-semibold text-gray-800">
                  {insights.lockCount} lock{insights.lockCount > 1 ? "s" : ""}
                </p>
                <p className="text-xs text-gray-400">Screen locked today</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── Top apps ── */}
      {insights.topApps.length > 0 && (
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-3">
          <div className="flex items-center justify-between mb-3">
            <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Applications
              <span className="ml-1.5 font-normal normal-case text-gray-300">
                ({insights.topApps.length})
              </span>
            </p>
            {insights.topApps.length > 5 && (
              <button
                onClick={() => setShowAllApps(v => !v)}
                className="flex items-center gap-1 text-xs text-brand-500 hover:text-brand-700 font-medium transition"
              >
                {showAllApps ? (
                  <><ChevronUp size={12} /> Show less</>
                ) : (
                  <><ChevronDown size={12} /> View all {insights.topApps.length}</>
                )}
              </button>
            )}
          </div>

          <div className={`space-y-3 ${showAllApps ? "max-h-72 overflow-y-auto pr-1" : ""}`}>
            {(showAllApps ? insights.topApps : insights.topApps.slice(0, 5)).map(([app, secs], i) => {
              const pct = Math.round((secs / maxAppSecs) * 100);
              return (
                <div key={app}>
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="text-xs font-bold text-gray-300 w-3.5 shrink-0">{i + 1}</span>
                      <span className="text-xs font-semibold text-gray-700 truncate">{app}</span>
                    </div>
                    <span className="text-xs text-gray-400 shrink-0 ml-1">{fmtSeconds(secs)}</span>
                  </div>
                  <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div className="h-full rounded-full bg-brand-400 transition-all" style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

    </div>
  );
}
