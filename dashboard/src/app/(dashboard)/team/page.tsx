"use client";
import PageLoader from "@/components/PageLoader";
import PageSkeleton from "@/components/PageSkeleton";
import { useState, useEffect } from "react";
import useSWR from "swr";
import { ChevronDown, RefreshCw, Users2, Wifi, Coffee, WifiOff } from "lucide-react";
import { getOverview, getHomeAppUsage, getGroups, fmtSeconds } from "@/lib/api";
import GroupPicker from "@/components/GroupPicker";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import { StatusDonut, ActivityTrend, UsagePie } from "@/components/charts";
import type { Group, OverviewStats, AppUsage } from "@/types";

export default function TeamPage() {
  const { loading, can } = useUser();
  const [groupId, setGroupId] = useState<string>("");

  const { data: groups } = useSWR<Group[]>("groups-for-team", getGroups);

  // Default to the first root group once loaded.
  useEffect(() => {
    if (!groupId && groups && groups.length) {
      const roots = groups.filter(g => !g.parent_id);
      setGroupId((roots[0] ?? groups[0]).id);
    }
  }, [groups, groupId]);

  const { data: stats, mutate } = useSWR<OverviewStats>(
    groupId ? ["team-overview", groupId] : null,
    () => getOverview(groupId, 7),
    { refreshInterval: 15_000 },
  );
  const { data: apps } = useSWR<AppUsage[]>(
    groupId ? ["team-apps", groupId] : null,
    () => getHomeAppUsage(groupId, 7),
  );

  if (loading) return <PageLoader />;
  if (!can("home", "view")) return <AccessDenied label="Team Management" />;
  // Cold load: wait for groups, then for the selected group's stats.
  if (!groups || (groupId && !stats)) return <PageSkeleton variant="dashboard" />;

  const s = stats?.status ?? { active: 0, passive: 0, inactive: 0 };
  const avgHrs = ((stats?.avg_active_seconds_today ?? 0) / 3600).toFixed(1);
  const selected = groups?.find(g => g.id === groupId);

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Team Management</h1>
        <button onClick={() => mutate()}
                className="flex items-center gap-2 text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm">
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      {/* Group selector */}
      <div className="flex items-center gap-3 my-4">
        <GroupPicker value={groupId} onChange={setGroupId} allowEmpty={false} emptyLabel="Select group" className="min-w-[200px]" />
        {selected && (
          <span className="text-sm text-gray-500">
            {selected.path ?? selected.name} · {selected.member_count} members
            {selected.child_count > 0 ? ` (+ subgroups)` : ""}
          </span>
        )}
      </div>

      {!groupId ? (
        <div className="bg-white border border-gray-200 rounded-xl p-12 text-center text-gray-600 text-sm shadow-sm">
          Create a group and add members to see team activity here.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
              <p className="text-2xl font-bold text-gray-900">{stats?.active_today ?? 0}<span className="text-sm text-gray-600">/{stats?.total_devices ?? 0}</span></p>
              <p className="text-xs text-gray-600 mt-0.5">Active today</p>
            </div>
            <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
              <p className="text-2xl font-bold text-brand-600">{avgHrs}h</p>
              <p className="text-xs text-gray-600 mt-0.5">Avg active / device</p>
            </div>
            <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
              <p className="text-2xl font-bold text-green-600">{s.active}</p>
              <p className="text-xs text-gray-600 mt-0.5">Active now</p>
            </div>
            <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
              <p className="text-2xl font-bold text-gray-600">{s.inactive}</p>
              <p className="text-xs text-gray-600 mt-0.5">Inactive now</p>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
            <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <p className="text-sm font-semibold text-gray-800 mb-3">Team Activity Status</p>
              <div className="grid grid-cols-2 gap-4 items-center">
                <StatusDonut active={s.active} passive={s.passive} inactive={s.inactive} />
                <div className="space-y-4">
                  <div className="flex items-center gap-3"><Wifi size={18} className="text-green-500" /><div><p className="text-xl font-bold text-gray-900 leading-none">{s.active}</p><p className="text-xs text-gray-600 mt-0.5">Active now</p></div></div>
                  <div className="flex items-center gap-3"><Coffee size={18} className="text-amber-500" /><div><p className="text-xl font-bold text-gray-900 leading-none">{s.passive}</p><p className="text-xs text-gray-600 mt-0.5">Passive now</p></div></div>
                  <div className="flex items-center gap-3"><WifiOff size={18} className="text-gray-600" /><div><p className="text-xl font-bold text-gray-900 leading-none">{s.inactive}</p><p className="text-xs text-gray-600 mt-0.5">Inactive now</p></div></div>
                </div>
              </div>
            </div>

            <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <p className="text-sm font-semibold text-gray-800 mb-3">Top Applications (7 days)</p>
              <UsagePie items={apps ?? []} labelKey="app_name" valueKey="seconds" format={fmtSeconds} />
            </div>
          </div>

          <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm mb-6">
            <p className="text-sm font-semibold text-gray-800 mb-3">Active Hours Trend (7 days)</p>
            <ActivityTrend data={stats?.trend ?? []} />
          </div>
        </>
      )}
    </div>
  );
}
