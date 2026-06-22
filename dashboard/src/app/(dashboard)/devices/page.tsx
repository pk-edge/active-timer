"use client";
import { useState, useMemo } from "react";
import useSWR from "swr";
import { getDevices, getUsers } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import { useDevicesPush } from "@/hooks/useDevicesPush";
import DeviceCard from "./_components/DeviceCard";
import { Monitor, Wifi, WifiOff, Search, X, Users2, Archive } from "lucide-react";
import type { Device, User } from "@/types";

type Filter = "all" | "online" | "offline";

function StatCard({
  label, value, icon, color = "text-gray-900", active, onClick,
}: {
  label: string; value: number; icon: React.ReactNode;
  color?: string; active?: boolean; onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`bg-white border rounded-xl p-4 flex items-center gap-4 w-full text-left transition ${
        active
          ? "border-brand-400 ring-2 ring-blue-100 shadow-sm"
          : "border-gray-200 hover:border-gray-300 hover:shadow-sm"
      }`}
    >
      <div className={`${active ? "text-brand-500" : "text-gray-400"}`}>{icon}</div>
      <div>
        <p className={`text-2xl font-semibold ${color}`}>{value}</p>
        <p className="text-gray-500 text-xs">{label}</p>
      </div>
    </button>
  );
}

function PageSkeleton() {
  return (
    <div className="animate-pulse space-y-4">
      <div className="h-8 w-48 bg-gray-200 rounded" />
      <div className="grid grid-cols-3 gap-4">
        {[...Array(3)].map((_, i) => <div key={i} className="h-20 bg-gray-200 rounded-xl" />)}
      </div>
      <div className="grid grid-cols-3 gap-4">
        {[...Array(6)].map((_, i) => <div key={i} className="h-28 bg-gray-200 rounded-xl" />)}
      </div>
    </div>
  );
}

export default function DevicesPage() {
  const { loading, can } = useUser();
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"all" | "byUser">("all");
  const [showArchived, setShowArchived] = useState(false);

  const canArchive = can("devices", "archive");
  const canDelete  = can("devices", "delete");

  useDevicesPush(showArchived);

  const swrKey = showArchived ? "devices?show_archived=true" : "devices";
  const { data: devices, isLoading, mutate } = useSWR<Device[]>(
    swrKey,
    () => getDevices(showArchived),
    { refreshInterval: 5_000, revalidateOnFocus: true, revalidateOnReconnect: true },
  );

  // Employees for the assign dropdown — needs users.view to populate.
  const canAssign = can("devices", "assign") && can("users", "view");
  const { data: users } = useSWR<User[]>(canAssign ? "users-for-assign" : null, () => getUsers());
  const employees = useMemo(
    () => (users ?? []).map(u => ({ id: u.id, name: u.name })),
    [users],
  );

  const online   = devices?.filter(d => d.is_online).length ?? 0;
  const archived = devices?.filter(d => d.is_archived).length ?? 0;
  const offline  = (devices?.filter(d => !d.is_online && !d.is_archived).length) ?? 0;

  const filtered = useMemo(() => {
    let list = devices ?? [];
    if (showArchived) {
      list = list.filter(d => d.is_archived);
    } else {
      if (filter === "online")  list = list.filter(d => d.is_online);
      if (filter === "offline") list = list.filter(d => !d.is_online);
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(
        d =>
          d.hostname.toLowerCase().includes(q) ||
          (d.employee_name ?? "").toLowerCase().includes(q) ||
          d.platform.toLowerCase().includes(q),
      );
    }
    return list;
  }, [devices, filter, search, showArchived]);

  const grouped = useMemo(() => {
    const m = new Map<string, { name: string; devices: Device[] }>();
    for (const d of filtered) {
      const key = d.employee_id ?? "__unassigned__";
      if (!m.has(key)) m.set(key, { name: d.employee_name ?? "Unassigned", devices: [] });
      m.get(key)!.devices.push(d);
    }
    return [...m.entries()].sort((a, b) =>
      a[0] === "__unassigned__" ? -1 :
      b[0] === "__unassigned__" ? 1 :
      a[1].name.localeCompare(b[1].name));
  }, [filtered]);

  if (loading) return null;
  if (!can("devices", "view")) return <AccessDenied label="Devices" />;
  if (isLoading) return <PageSkeleton />;

  const activeCount = (devices?.length ?? 0) - archived;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-1">
        <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">All Devices</h1>
        <div className="flex items-center gap-3">
          {canArchive && (
            <button
              onClick={() => { setShowArchived(v => !v); setFilter("all"); }}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border transition ${
                showArchived
                  ? "bg-amber-50 border-amber-300 text-amber-700"
                  : "bg-white border-gray-200 text-gray-500 hover:border-gray-300"
              }`}
            >
              <Archive size={14} />
              {showArchived ? "Hide archived" : "Show archived"}
            </button>
          )}
          <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden">
            <button onClick={() => setView("all")}
                    className={`px-3 py-1.5 text-sm font-medium transition ${view === "all" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500"}`}>
              All devices
            </button>
            <button onClick={() => setView("byUser")}
                    className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium transition ${view === "byUser" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500"}`}>
              <Users2 size={14} /> By user
            </button>
          </div>
        </div>
      </div>
      <p className="text-gray-500 text-sm mb-6">Live — refreshes every 5 seconds</p>

      {/* Stat cards */}
      <div className={`grid gap-4 mb-6 ${showArchived && canArchive ? "grid-cols-4" : "grid-cols-3"}`}>
        <StatCard
          label="Active devices"
          value={activeCount}
          icon={<Monitor size={18} />}
          active={filter === "all"}
          onClick={() => setFilter("all")}
        />
        <StatCard
          label="Online"
          value={online}
          icon={<Wifi size={18} />}
          color="text-green-600"
          active={filter === "online"}
          onClick={() => setFilter("online")}
        />
        <StatCard
          label="Offline"
          value={offline}
          icon={<WifiOff size={18} />}
          color="text-gray-500"
          active={filter === "offline"}
          onClick={() => setFilter("offline")}
        />
        {showArchived && canArchive && (
          <StatCard
            label="Archived"
            value={archived}
            icon={<Archive size={18} />}
            color="text-amber-600"
          />
        )}
      </div>

      {/* Search bar */}
      <div className="relative mb-5">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
        <input
          type="text"
          placeholder="Search by hostname, employee or platform…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full pl-9 pr-9 py-2.5 bg-white border border-gray-300 rounded-lg text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
        />
        {search && (
          <button
            onClick={() => setSearch("")}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {/* Device grid */}
      {view === "all" && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
          {filtered.map(d => (
            <DeviceCard
              key={d.id}
              device={d}
              employees={employees}
              canAssign={canAssign}
              canArchive={canArchive}
              canDelete={canDelete}
              onAssigned={() => mutate()}
              onArchived={() => mutate()}
              onRestored={() => mutate()}
              onDeleted={() => mutate()}
            />
          ))}
          {filtered.length === 0 && (
            <p className="col-span-full text-gray-400 text-sm py-12 text-center">
              {search || filter !== "all"
                ? "No devices match your filter."
                : "No devices enrolled yet. Install the agent on a laptop to get started."}
            </p>
          )}
        </div>
      )}

      {/* Grouped by user */}
      {view === "byUser" && (
        <div className="space-y-6">
          {grouped.map(([key, group]) => (
            <div key={key}>
              <div className="flex items-center gap-2 mb-2.5">
                <p className={`text-sm font-semibold ${key === "__unassigned__" ? "text-amber-600" : "text-gray-800"}`}>
                  {group.name}
                </p>
                <span className="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">
                  {group.devices.length} device{group.devices.length !== 1 ? "s" : ""}
                </span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
                {group.devices.map(d => (
                  <DeviceCard
                    key={d.id}
                    device={d}
                    employees={employees}
                    canAssign={canAssign}
                    canArchive={canArchive}
                    canDelete={canDelete}
                    onAssigned={() => mutate()}
                    onArchived={() => mutate()}
                    onRestored={() => mutate()}
                    onDeleted={() => mutate()}
                  />
                ))}
              </div>
            </div>
          ))}
          {grouped.length === 0 && (
            <p className="text-gray-400 text-sm py-12 text-center">No devices match your filter.</p>
          )}
        </div>
      )}

      {search || filter !== "all" ? (
        <p className="text-xs text-gray-400 mt-3">
          {filtered.length} of {devices?.length ?? 0} device{devices?.length !== 1 ? "s" : ""}
        </p>
      ) : null}
    </div>
  );
}
