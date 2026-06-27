"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useMemo } from "react";
import Link from "next/link";
import useSWR from "swr";
import {
  DownloadCloud, FileDown, Printer, ChevronDown,
  Monitor, Globe, Search, X,
} from "lucide-react";
import { getHomeAppUsage, getHomeWebUsage, getGroups, fmtSeconds, fmtTime } from "@/lib/api";
import { useRealtimeStream } from "@/hooks/useRealtimeStream";
import GroupPicker from "@/components/GroupPicker";
import { buildGroupTree } from "@/lib/groupTree";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { smartMatch } from "@/lib/smartSearch";
import { AccessDenied } from "@/components/AccessDenied";
import PageSkeleton from "@/components/PageSkeleton";
import { UsagePie } from "@/components/charts";
import type { RealtimeDevice, Group, AppUsage, WebUsage } from "@/types";

const STATUS_STYLE: Record<string, string> = {
  active:   "bg-green-50 text-green-600 border-green-200",
  passive:  "bg-amber-50 text-amber-600 border-amber-200",
  inactive: "bg-gray-100 text-gray-600 border-gray-200",
};

function DeviceTile({ d }: { d: RealtimeDevice }) {
  return (
    <Link href={`/devices/${d.device_id}`}
          className="bg-white border border-gray-200 rounded-xl p-3 hover:border-brand-300 hover:shadow-sm transition block">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium text-gray-900 truncate">{d.employee_name ?? d.hostname}</p>
        <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full border shrink-0 capitalize ${STATUS_STYLE[d.status]}`}>
          {d.status}
        </span>
      </div>
      <p className="text-xs text-gray-600 truncate mt-0.5">{d.hostname}</p>
      <p className="text-xs mt-2 truncate">
        {d.current_app
          ? <span className="text-brand-600 font-medium">{d.current_app}</span>
          : <span className="text-gray-600">{d.last_seen_unix ? `Last seen ${fmtTime(d.last_seen_unix)}` : "Never seen"}</span>}
      </p>
    </Link>
  );
}

function exportCsv(rows: RealtimeDevice[]) {
  const header = ["hostname", "employee", "platform", "status", "current_app", "last_seen_unix"];
  const body = rows.map(r => [r.hostname, r.employee_name ?? "", r.platform, r.status, r.current_app ?? "", r.last_seen_unix ?? ""]
    .map(v => `"${String(v).replace(/"/g, '""')}"`).join(","));
  const csv = [header.join(","), ...body].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "activity-export.csv"; a.click();
  URL.revokeObjectURL(url);
}

export default function ActivityPage() {
  const { loading, can } = useUser();
  const [tab, setTab]   = useState<"users" | "groups">("users");
  const [groupId, setGroup] = useState("");
  const [search, setSearch] = useState("");

  // ── Realtime via SSE — one persistent connection replaces a 10 s HTTP poll.
  // The stream endpoint pushes updates whenever ingest lands new events, so
  // the device grid stays live without any recurring requests.
  const realtime = useRealtimeStream(groupId || undefined);
  const hasRealtime = realtime.length > 0;

  // ── Groups — only fetch when the user switches to the Groups tab.
  // On the default Users tab this request never fires.
  const { data: groups } = useSWR<Group[]>(
    tab === "groups" ? "groups-activity" : null,
    getGroups,
    { dedupingInterval: 60_000 },
  );

  // ── Usage charts — deferred until the realtime grid has painted.
  // Staggering these avoids a 4-request burst on cold mount; SWR cache makes
  // them instant on subsequent visits regardless.
  const { data: apps } = useSWR<AppUsage[]>(
    hasRealtime ? ["act-apps", groupId] : null,
    () => getHomeAppUsage(groupId || undefined, 1),
    { dedupingInterval: 60_000 },
  );
  const { data: webs } = useSWR<WebUsage[]>(
    hasRealtime ? ["act-webs", groupId] : null,
    () => getHomeWebUsage(groupId || undefined, 1),
    { dedupingInterval: 60_000 },
  );

  const debouncedSearch = useDebouncedValue(search, 200);
  const filtered = useMemo(() => {
    if (!debouncedSearch.trim()) return realtime;
    return realtime.filter(d => smartMatch(debouncedSearch, d.hostname, d.employee_name));
  }, [realtime, debouncedSearch]);

  if (loading) return <PageLoader />;
  if (!can("home", "view")) return <AccessDenied label="the Activity Dashboard" />;
  // Cold load: skeleton while the SSE connection delivers the first snapshot.
  // keepPreviousData (global SWR config) serves the cache on re-navigation,
  // so the skeleton only appears on the very first visit.
  if (!hasRealtime && !loading) return <PageSkeleton variant="dashboard" />;

  const counts = {
    active:   realtime.filter(d => d.status === "active").length,
    passive:  realtime.filter(d => d.status === "passive").length,
    inactive: realtime.filter(d => d.status === "inactive").length,
  };

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      {/* Header + actions */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Activity Dashboard</h1>
        <div className="flex items-center gap-2">
          <button onClick={() => exportCsv(filtered)} className="flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm">
            <FileDown size={13} /> Export
          </button>
          <button onClick={() => window.print()} className="flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm">
            <Printer size={13} /> Print
          </button>
          {can("agents", "view") && (
            <Link href="/agents" className="flex items-center gap-1.5 text-sm text-white bg-brand-600 hover:bg-brand-700 px-3 py-1.5 rounded-lg shadow-sm">
              <DownloadCloud size={13} /> Agent
            </Link>
          )}
        </div>
      </div>

      {/* Filters row */}
      <div className="flex flex-wrap items-center gap-3 mb-5">
        <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden">
          {(["users", "groups"] as const).map(t => (
            <button key={t} onClick={() => setTab(t)}
                    className={`px-3 py-1.5 text-sm capitalize transition ${tab === t ? "bg-white text-gray-900 shadow-sm font-medium" : "text-gray-600"}`}>
              {t}
            </button>
          ))}
        </div>
        <GroupPicker value={groupId} onChange={setGroup} emptyLabel="All groups" />
        {tab === "users" && (
          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search user or device…"
                   className="w-full pl-9 pr-8 py-1.5 bg-white border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
            {search && <button onClick={() => setSearch("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600"><X size={13} /></button>}
          </div>
        )}
        <div className="ml-auto flex items-center gap-3 text-xs">
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-green-500" />{counts.active} active</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-amber-500" />{counts.passive} passive</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-gray-300" />{counts.inactive} inactive</span>
        </div>
      </div>

      {/* Realtime visibility */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm mb-6 overflow-hidden">
        <div className="px-4 pt-4 pb-2 border-b border-gray-100 flex items-center justify-between">
          <p className="text-sm font-semibold text-gray-800">Today&apos;s Real-time Visibility</p>
          <span className="text-xs text-gray-600">{filtered.length} devices</span>
        </div>
        {/* Scrollable grid — max-h so it doesn't push the rest of the page */}
        <div className="overflow-y-auto max-h-72 p-4">
          {tab === "users" ? (
            filtered.length === 0 ? (
              <p className="text-sm text-gray-600 py-8 text-center">No devices match.</p>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3">
                {filtered.map(d => <DeviceTile key={d.device_id} d={d} />)}
              </div>
            )
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3">
              {buildGroupTree(groups ?? []).map(node => (
                <Link key={node.group.id} href={`/groups/${node.group.id}`}
                  className="bg-white border border-gray-200 rounded-xl p-3 hover:border-brand-300 hover:shadow-sm transition block">
                  <p className="text-sm font-medium text-gray-900 truncate">{node.group.name}</p>
                  <p className="text-xs text-gray-500 mt-1">
                    {node.group.member_count} members
                    {node.children.length > 0 ? ` · ${node.children.length} subgroups` : ""}
                  </p>
                </Link>
              ))}
              {(groups ?? []).length === 0 && <p className="col-span-full text-sm text-gray-600 py-8 text-center">No groups yet.</p>}
            </div>
          )}
        </div>
      </div>

      {/* Usage charts — render skeleton until deferred data arrives */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3"><Monitor size={15} className="text-gray-600" /><p className="text-sm font-semibold text-gray-800">Applications (today)</p></div>
          {apps
            ? <UsagePie items={apps} labelKey="app_name" valueKey="seconds" format={fmtSeconds} />
            : <div className="h-[220px] animate-pulse bg-gray-50 rounded-full mx-auto w-[220px]" />}
        </div>
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-3"><Globe size={15} className="text-gray-600" /><p className="text-sm font-semibold text-gray-800">Websites (today)</p></div>
          {webs
            ? <UsagePie items={webs} labelKey="host" valueKey="visits" format={(v) => `${v} visits`} />
            : <div className="h-[220px] animate-pulse bg-gray-50 rounded-full mx-auto w-[220px]" />}
        </div>
      </div>
    </div>
  );
}
