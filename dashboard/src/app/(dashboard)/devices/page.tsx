"use client";
import { useState, useMemo } from "react";
import useSWR from "swr";
import { getDevices } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import { useDevicesPush } from "@/hooks/useDevicesPush";
import DeviceCard from "./_components/DeviceCard";
import { Monitor, Wifi, WifiOff, Search, X } from "lucide-react";
import type { Device } from "@/types";

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
  useDevicesPush();
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");

  const { data: devices, isLoading } = useSWR<Device[]>("devices", getDevices, {
    refreshInterval: 5_000,
    revalidateOnFocus: true,
    revalidateOnReconnect: true,
  });

  const online  = devices?.filter(d => d.is_online).length ?? 0;
  const offline = (devices?.length ?? 0) - online;

  const filtered = useMemo(() => {
    let list = devices ?? [];
    if (filter === "online")  list = list.filter(d => d.is_online);
    if (filter === "offline") list = list.filter(d => !d.is_online);
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
  }, [devices, filter, search]);

  if (loading) return null;
  if (!can("devices", "view")) return <AccessDenied label="Devices" />;
  if (isLoading) return <PageSkeleton />;

  return (
    <div>
      <h1 className="text-xl sm:text-2xl font-semibold text-gray-900 mb-1">All Devices</h1>
      <p className="text-gray-500 text-sm mb-6">Live — refreshes every 5 seconds</p>

      {/* Stat cards — clickable filter */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <StatCard
          label="Total devices"
          value={devices?.length ?? 0}
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
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
        {filtered.map(d => <DeviceCard key={d.id} device={d} />)}
        {filtered.length === 0 && (
          <p className="col-span-full text-gray-400 text-sm py-12 text-center">
            {search || filter !== "all"
              ? "No devices match your filter."
              : "No devices enrolled yet. Install the agent on a laptop to get started."}
          </p>
        )}
      </div>

      {search || filter !== "all" ? (
        <p className="text-xs text-gray-400 mt-3">{filtered.length} of {devices?.length ?? 0} device{devices?.length !== 1 ? "s" : ""}</p>
      ) : null}
    </div>
  );
}
