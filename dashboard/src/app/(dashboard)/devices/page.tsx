"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useMemo, useEffect } from "react";
import useSWR from "swr";
import Link from "next/link";
import { formatDistanceToNow } from "date-fns";
import {
  getDevices, getUsers,
  assignDevice, archiveDevice, restoreDevice, deleteDevice,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { smartMatch } from "@/lib/smartSearch";
import { AccessDenied } from "@/components/charts";
import { useDevicesPush } from "@/hooks/useDevicesPush";
import {
  Monitor, Wifi, WifiOff, Search, X, Users2, Archive,
  RotateCcw, Trash2, UserRound, ChevronLeft, ChevronRight,
} from "lucide-react";
import type { Device, User } from "@/types";

type Filter = "all" | "online" | "offline";

const PAGE_SIZE = 25;

// ── Stat card (clickable filter) ─────────────────────────────────────────────

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
      <div className={active ? "text-brand-500" : "text-gray-600"}>{icon}</div>
      <div>
        <p className={`text-2xl font-semibold ${color}`}>{value}</p>
        <p className="text-gray-600 text-xs">{label}</p>
      </div>
    </button>
  );
}

// ── Loading skeleton ──────────────────────────────────────────────────────────

function TableSkeleton() {
  return (
    <div className="animate-pulse space-y-4">
      <div className="h-8 w-48 bg-gray-200 rounded" />
      <div className="grid grid-cols-3 gap-4">
        {[...Array(3)].map((_, i) => <div key={i} className="h-20 bg-gray-200 rounded-xl" />)}
      </div>
      <div className="h-10 bg-gray-200 rounded-lg" />
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
        <div className="h-10 bg-gray-50 border-b border-gray-200" />
        {[...Array(7)].map((_, i) => (
          <div key={i} className="flex items-center gap-6 px-4 py-3.5 border-b border-gray-100">
            <div className="h-4 w-36 bg-gray-200 rounded" />
            <div className="h-5 w-16 bg-gray-200 rounded-full" />
            <div className="h-4 w-28 bg-gray-200 rounded" />
            <div className="h-4 w-16 bg-gray-200 rounded" />
            <div className="h-4 w-20 bg-gray-200 rounded" />
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Pagination ────────────────────────────────────────────────────────────────

function pageNumbers(current: number, total: number): (number | "...")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: (number | "...")[] = [1];
  if (current > 3) pages.push("...");
  const start = Math.max(2, current - 1);
  const end   = Math.min(total - 1, current + 1);
  for (let i = start; i <= end; i++) pages.push(i);
  if (current < total - 2) pages.push("...");
  pages.push(total);
  return pages;
}

function Pagination({
  page, totalPages, total, onPage,
}: { page: number; totalPages: number; total: number; onPage: (p: number) => void }) {
  const [jumpVal, setJumpVal] = useState("");

  function handleJump(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key !== "Enter") return;
    const n = parseInt(jumpVal, 10);
    if (!isNaN(n) && n >= 1 && n <= totalPages) {
      onPage(n);
      setJumpVal("");
    }
  }

  if (total === 0) return null;

  const start = (page - 1) * PAGE_SIZE + 1;
  const end   = Math.min(page * PAGE_SIZE, total);
  const pages = pageNumbers(page, totalPages);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 mt-4 pt-4 border-t border-gray-100">
      <p className="text-sm text-gray-500">
        Showing {start}–{end} of {total} device{total !== 1 ? "s" : ""}
      </p>

      {totalPages > 1 && (
        <div className="flex items-center gap-2">
          {/* Previous */}
          <button
            onClick={() => onPage(page - 1)}
            disabled={page === 1}
            className="flex items-center gap-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg text-gray-700 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            <ChevronLeft size={14} /> Previous
          </button>

          {/* Page numbers */}
          <div className="flex items-center gap-1">
            {pages.map((p, i) =>
              p === "..." ? (
                <span key={`ellipsis-${i}`} className="px-1 text-gray-400 text-sm select-none">…</span>
              ) : (
                <button
                  key={p}
                  onClick={() => onPage(p as number)}
                  className={`w-8 h-8 text-sm rounded-lg transition ${
                    p === page
                      ? "bg-brand-500 text-white font-medium"
                      : "text-gray-700 hover:bg-gray-100"
                  }`}
                >
                  {p}
                </button>
              )
            )}
          </div>

          {/* Jump to page */}
          <div className="flex items-center gap-1.5 ml-1">
            <span className="text-sm text-gray-500">Go to</span>
            <input
              type="number"
              min={1}
              max={totalPages}
              value={jumpVal}
              onChange={e => setJumpVal(e.target.value)}
              onKeyDown={handleJump}
              placeholder="—"
              className="w-14 text-center text-sm border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-brand-300"
            />
          </div>

          {/* Next */}
          <button
            onClick={() => onPage(page + 1)}
            disabled={page === totalPages}
            className="flex items-center gap-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg text-gray-700 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            Next <ChevronRight size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

// ── Device table row ──────────────────────────────────────────────────────────

function DeviceRow({
  device, employees, canAssign, canArchive, canDelete,
  onAssigned, onArchived, onRestored, onDeleted,
}: {
  device: Device;
  employees: { id: string; name: string }[];
  canAssign: boolean;
  canArchive: boolean;
  canDelete: boolean;
  onAssigned: () => void;
  onArchived: () => void;
  onRestored: () => void;
  onDeleted: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const showActions = canArchive || canDelete;

  const lastSeen = device.last_seen_unix
    ? formatDistanceToNow(new Date(device.last_seen_unix * 1000), { addSuffix: true })
    : "never";

  async function handleAssign(employeeId: string) {
    setSaving(true);
    try {
      await assignDevice(device.id, employeeId || null);
      onAssigned();
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setSaving(false); }
  }

  async function handleArchive() {
    setSaving(true);
    try {
      await archiveDevice(device.id);
      onArchived();
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setSaving(false); }
  }

  async function handleRestore() {
    setSaving(true);
    try {
      await restoreDevice(device.id);
      onRestored();
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setSaving(false); }
  }

  async function handleDelete() {
    if (!confirm(`Permanently delete "${device.hostname}"? If the agent is still running it can re-register.`)) return;
    setSaving(true);
    try {
      await deleteDevice(device.id);
      onDeleted();
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setSaving(false); }
  }

  return (
    <tr className={`border-b border-gray-100 last:border-0 hover:bg-gray-50/70 transition-colors ${device.is_archived ? "bg-amber-50/20" : ""}`}>

      {/* Hostname */}
      <td className="px-4 py-3.5">
        <Link
          href={`/devices/${device.id}`}
          className="font-medium text-sm text-gray-900 hover:text-brand-600 transition-colors"
        >
          {device.hostname}
        </Link>
      </td>

      {/* Status */}
      <td className="px-4 py-3.5">
        {device.is_archived ? (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium px-2 py-0.5 rounded-full bg-amber-50 text-amber-600 border border-amber-200">
            <Archive size={10} /> Archived
          </span>
        ) : device.is_online ? (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium px-2 py-0.5 rounded-full bg-green-50 text-green-600 border border-green-200">
            <span className="relative flex items-center justify-center w-1.5 h-1.5">
              <span className="absolute inline-flex w-full h-full rounded-full bg-green-400 opacity-75 animate-ping" />
              <span className="relative rounded-full w-1.5 h-1.5 bg-green-500" />
            </span>
            Online
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 border border-gray-200">
            <span className="rounded-full w-1.5 h-1.5 bg-gray-400 shrink-0" />
            Offline
          </span>
        )}
      </td>

      {/* Employee */}
      <td className="px-4 py-3.5">
        {canAssign && !device.is_archived ? (
          <div className="flex items-center gap-1.5">
            <UserRound size={12} className="text-gray-400 shrink-0" />
            <select
              value={device.employee_id ?? ""}
              disabled={saving}
              onChange={e => handleAssign(e.target.value)}
              className="text-xs border border-gray-200 rounded-md px-2 py-1 text-gray-700 bg-white focus:outline-none focus:ring-1 focus:ring-brand-300 disabled:opacity-50 max-w-[160px]"
            >
              <option value="">Unassigned</option>
              {employees.map(emp => (
                <option key={emp.id} value={emp.id}>{emp.name}</option>
              ))}
            </select>
          </div>
        ) : (
          <span className={`text-sm ${device.employee_name ? "text-gray-700" : "text-gray-400 italic"}`}>
            {device.employee_name ?? "Unassigned"}
          </span>
        )}
      </td>

      {/* Platform */}
      <td className="px-4 py-3.5">
        <span className="text-sm text-gray-600 capitalize">{device.platform}</span>
      </td>

      {/* Last seen */}
      <td className="px-4 py-3.5">
        {device.is_online
          ? <span className="text-sm text-green-600 font-medium">Active now</span>
          : <span className="text-sm text-gray-500">{lastSeen}</span>
        }
      </td>

      {/* Actions */}
      {showActions && (
        <td className="px-4 py-3.5">
          <div className="flex items-center gap-1">
            {canArchive && !device.is_archived && (
              <button
                onClick={handleArchive}
                disabled={saving}
                className="inline-flex items-center gap-1 text-xs text-amber-600 hover:text-amber-700 hover:bg-amber-50 px-2 py-1 rounded transition disabled:opacity-40"
              >
                <Archive size={11} /> Archive
              </button>
            )}
            {canArchive && device.is_archived && (
              <button
                onClick={handleRestore}
                disabled={saving}
                className="inline-flex items-center gap-1 text-xs text-green-600 hover:text-green-700 hover:bg-green-50 px-2 py-1 rounded transition disabled:opacity-40"
              >
                <RotateCcw size={11} /> Restore
              </button>
            )}
            {canDelete && (
              <button
                onClick={handleDelete}
                disabled={saving}
                className="inline-flex items-center gap-1 text-xs text-red-500 hover:text-red-600 hover:bg-red-50 px-2 py-1 rounded transition disabled:opacity-40"
              >
                <Trash2 size={11} /> Delete
              </button>
            )}
          </div>
        </td>
      )}
    </tr>
  );
}

// ── Device table wrapper ──────────────────────────────────────────────────────

type RowCallbacks = {
  employees: { id: string; name: string }[];
  canAssign: boolean;
  canArchive: boolean;
  canDelete: boolean;
  onAssigned: () => void;
  onArchived: () => void;
  onRestored: () => void;
  onDeleted: () => void;
};

function DeviceTable({ devices, ...row }: { devices: Device[] } & RowCallbacks) {
  const showActions = row.canArchive || row.canDelete;
  const colCount = 5 + (showActions ? 1 : 0);

  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white">
      <table className="w-full min-w-[640px]">
        <thead>
          <tr className="bg-gray-50 border-b border-gray-200">
            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Hostname</th>
            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Status</th>
            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Employee</th>
            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Platform</th>
            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Last Seen</th>
            {showActions && (
              <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Actions</th>
            )}
          </tr>
        </thead>
        <tbody>
          {devices.map(d => (
            <DeviceRow key={d.id} device={d} {...row} />
          ))}
          {devices.length === 0 && (
            <tr>
              <td colSpan={colCount} className="px-4 py-14 text-center text-sm text-gray-400">
                No devices match your filter.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function DevicesPage() {
  const { loading, can } = useUser();
  const [filter, setFilter]           = useState<Filter>("all");
  const [search, setSearch]           = useState("");
  const [view, setView]               = useState<"all" | "byUser">("all");
  const [showArchived, setShowArchived] = useState(false);
  const [page, setPage]               = useState(1);

  const canArchive  = can("devices", "archive");
  const canDelete   = can("devices", "delete");
  const canAssign   = can("devices", "assign") && can("users", "view");
  const showActions = canArchive || canDelete;

  useDevicesPush(showArchived);

  const swrKey = showArchived ? "devices?show_archived=true" : "devices";
  const { data: devices, isLoading, mutate } = useSWR<Device[]>(
    swrKey,
    () => getDevices(showArchived),
    { refreshInterval: 5_000, revalidateOnFocus: true, revalidateOnReconnect: true },
  );

  const { data: users } = useSWR<User[]>(canAssign ? "users-for-assign" : null, () => getUsers());
  const employees = useMemo(() => (users ?? []).map(u => ({ id: u.id, name: u.name })), [users]);

  const online   = devices?.filter(d => d.is_online).length ?? 0;
  const archived = devices?.filter(d => d.is_archived).length ?? 0;
  const offline  = devices?.filter(d => !d.is_online && !d.is_archived).length ?? 0;

  const debouncedSearch = useDebouncedValue(search, 200);

  const filtered = useMemo(() => {
    let list = devices ?? [];
    if (showArchived) {
      list = list.filter(d => d.is_archived);
    } else {
      if (filter === "online")  list = list.filter(d => d.is_online);
      if (filter === "offline") list = list.filter(d => !d.is_online);
    }
    if (debouncedSearch.trim()) {
      list = list.filter(d => smartMatch(debouncedSearch, d.hostname, d.employee_name, d.platform));
    }
    return list;
  }, [devices, filter, debouncedSearch, showArchived]);

  // Reset to page 1 whenever the filtered set changes identity (search/filter/view)
  useEffect(() => { setPage(1); }, [filter, debouncedSearch, showArchived, view]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage   = Math.max(1, Math.min(page, totalPages));
  const paginated  = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const grouped = useMemo(() => {
    if (view !== "byUser") return [];
    const m = new Map<string, { name: string; devices: Device[] }>();
    for (const d of paginated) {
      const key = d.employee_id ?? "__unassigned__";
      if (!m.has(key)) m.set(key, { name: d.employee_name ?? "Unassigned", devices: [] });
      m.get(key)!.devices.push(d);
    }
    return [...m.entries()].sort((a, b) =>
      a[0] === "__unassigned__" ? -1 :
      b[0] === "__unassigned__" ? 1 :
      a[1].name.localeCompare(b[1].name),
    );
  }, [paginated, view]);

  if (loading)                       return <PageLoader />;
  if (!can("devices", "view"))       return <AccessDenied label="Devices" />;
  if (isLoading)                     return <TableSkeleton />;

  const activeCount = (devices?.length ?? 0) - archived;

  const rowProps: RowCallbacks = {
    employees,
    canAssign,
    canArchive,
    canDelete,
    onAssigned: () => mutate(),
    onArchived: () => mutate(),
    onRestored: () => mutate(),
    onDeleted:  () => mutate(),
  };

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">

      {/* ── Header ── */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-1">
        <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">All Devices</h1>

        <div className="flex items-center gap-3">
          {canArchive && (
            <button
              onClick={() => { setShowArchived(v => !v); setFilter("all"); }}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border transition ${
                showArchived
                  ? "bg-amber-50 border-amber-300 text-amber-700"
                  : "bg-white border-gray-200 text-gray-600 hover:border-gray-300"
              }`}
            >
              <Archive size={14} />
              {showArchived ? "Hide archived" : "Show archived"}
            </button>
          )}

          <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden p-0.5 gap-0.5">
            <button
              onClick={() => setView("all")}
              className={`px-3 py-1.5 text-sm font-medium rounded-md transition ${
                view === "all" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600 hover:text-gray-800"
              }`}
            >
              All devices
            </button>
            <button
              onClick={() => setView("byUser")}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md transition ${
                view === "byUser" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600 hover:text-gray-800"
              }`}
            >
              <Users2 size={14} /> By user
            </button>
          </div>
        </div>
      </div>
      <p className="text-gray-500 text-sm mb-6">Live — refreshes every 5 seconds</p>

      {/* ── Stat cards (global — not part of pagination) ── */}
      <div className={`grid gap-4 mb-6 ${showArchived && canArchive ? "grid-cols-4" : "grid-cols-3"}`}>
        <StatCard
          label="Active devices"
          value={activeCount}
          icon={<Monitor size={18} />}
          active={filter === "all" && !showArchived}
          onClick={() => { if (!showArchived) setFilter("all"); }}
        />
        <StatCard
          label="Online"
          value={online}
          icon={<Wifi size={18} />}
          color="text-green-600"
          active={filter === "online"}
          onClick={() => { if (!showArchived) setFilter("online"); }}
        />
        <StatCard
          label="Offline"
          value={offline}
          icon={<WifiOff size={18} />}
          color="text-gray-600"
          active={filter === "offline"}
          onClick={() => { if (!showArchived) setFilter("offline"); }}
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

      {/* ── Search bar (global — not part of pagination) ── */}
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

      {/* ── All-devices table ── */}
      {view === "all" && (
        <>
          {filtered.length === 0 && !search && filter === "all" ? (
            <div className="bg-white border border-gray-200 rounded-xl px-4 py-14 text-center text-sm text-gray-400">
              No devices enrolled yet. Install the agent on a laptop to get started.
            </div>
          ) : (
            <DeviceTable devices={paginated} {...rowProps} />
          )}
          <Pagination
            page={safePage}
            totalPages={totalPages}
            total={filtered.length}
            onPage={setPage}
          />
        </>
      )}

      {/* ── By-user grouped table ── */}
      {view === "byUser" && (
        <>
          {grouped.length === 0 ? (
            <div className="bg-white border border-gray-200 rounded-xl px-4 py-14 text-center text-sm text-gray-400">
              {search || filter !== "all"
                ? "No devices match your filter."
                : "No devices enrolled yet."}
            </div>
          ) : (
            <div className="space-y-6">
              {grouped.map(([key, group]) => (
                <div key={key}>
                  <div className="flex items-center gap-2 mb-2.5">
                    <p className={`text-sm font-semibold ${key === "__unassigned__" ? "text-amber-600" : "text-gray-800"}`}>
                      {group.name}
                    </p>
                    <span className="text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">
                      {group.devices.length} device{group.devices.length !== 1 ? "s" : ""}
                    </span>
                  </div>
                  <DeviceTable devices={group.devices} {...rowProps} />
                </div>
              ))}
            </div>
          )}
          <Pagination
            page={safePage}
            totalPages={totalPages}
            total={filtered.length}
            onPage={setPage}
          />
        </>
      )}
    </div>
  );
}
