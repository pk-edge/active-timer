"use client";
import { useState } from "react";
import useSWR from "swr";
import { RefreshCw, Wifi, Coffee, WifiOff, Clock, Activity as ActivityIcon } from "lucide-react";
import { getOverview, fmtSeconds } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied, StatusDonut, ActivityTrend } from "@/components/charts";
import type { OverviewStats } from "@/types";

const PERIODS = [{ label: "7 days", value: 7 }, { label: "14 days", value: 14 }, { label: "30 days", value: 30 }];

function StatusRow({ icon, label, value, color }: { icon: React.ReactNode; label: string; value: number; color: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className={color}>{icon}</span>
      <div>
        <p className="text-xl font-bold text-gray-900 leading-none">{value}</p>
        <p className="text-xs text-gray-500 mt-0.5">{label}</p>
      </div>
    </div>
  );
}

export default function OverviewPage() {
  const { loading, can } = useUser();
  const [days, setDays] = useState(7);
  const { data, isLoading, mutate } = useSWR<OverviewStats>(
    ["overview", days],
    () => getOverview(undefined, days),
    { refreshInterval: 15_000 },
  );

  if (loading) return null;
  if (!can("home", "view")) return <AccessDenied label="the Organization Overview" />;

  const s = data?.status ?? { active: 0, passive: 0, inactive: 0 };
  const avgHrs = ((data?.avg_active_seconds_today ?? 0) / 3600).toFixed(1);

  return (
    <div className="overflow-y-auto pr-1">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-2xl font-semibold text-gray-900">Organization Overview</h1>
        <button onClick={() => mutate()}
                className="flex items-center gap-2 text-sm text-gray-500 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm">
          <RefreshCw size={13} /> Refresh
        </button>
      </div>
      <p className="text-gray-500 text-sm mb-6">
        Live activity across all devices · <span className="text-gray-700 font-medium">{avgHrs} avg active hours today</span>
      </p>

      {/* Top stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
          <p className="text-2xl font-bold text-gray-900">{data?.total_devices ?? 0}</p>
          <p className="text-xs text-gray-500 mt-0.5">Total devices</p>
        </div>
        <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
          <p className="text-2xl font-bold text-green-600">{data?.active_today ?? 0}</p>
          <p className="text-xs text-gray-500 mt-0.5">Active today</p>
        </div>
        <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
          <p className="text-2xl font-bold text-blue-600">{avgHrs}h</p>
          <p className="text-xs text-gray-500 mt-0.5">Avg active / device</p>
        </div>
        <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
          <p className="text-2xl font-bold text-gray-900">{s.active}</p>
          <p className="text-xs text-gray-500 mt-0.5">Active right now</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* Activity donut + live status */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3">
            <ActivityIcon size={15} className="text-gray-400" />
            <p className="text-sm font-semibold text-gray-800">Live Activity Status</p>
          </div>
          <div className="grid grid-cols-2 gap-4 items-center">
            <StatusDonut active={s.active} passive={s.passive} inactive={s.inactive} />
            <div className="space-y-4">
              <StatusRow icon={<Wifi size={18} />}    label="Active now"   value={s.active}   color="text-green-500" />
              <StatusRow icon={<Coffee size={18} />}  label="Passive now"  value={s.passive}  color="text-amber-500" />
              <StatusRow icon={<WifiOff size={18} />} label="Inactive now" value={s.inactive} color="text-gray-400" />
            </div>
          </div>
        </div>

        {/* Active hours trend */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Clock size={15} className="text-gray-400" />
              <p className="text-sm font-semibold text-gray-800">Active Hours Trend</p>
            </div>
            <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden">
              {PERIODS.map(p => (
                <button key={p.value} onClick={() => setDays(p.value)}
                        className={`px-2.5 py-1 text-xs transition ${days === p.value ? "bg-blue-600 text-white" : "text-gray-600 hover:bg-gray-200"}`}>
                  {p.label}
                </button>
              ))}
            </div>
          </div>
          {isLoading ? <div className="h-[180px] animate-pulse bg-gray-50 rounded" /> : <ActivityTrend data={data?.trend ?? []} />}
        </div>
      </div>
    </div>
  );
}
