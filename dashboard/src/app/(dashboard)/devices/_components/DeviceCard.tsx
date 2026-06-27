"use client";
import { useState } from "react";
import Link from "next/link";
import { formatDistanceToNow } from "date-fns";
import { Clock, UserRound, Archive, RotateCcw, Trash2 } from "lucide-react";
import { assignDevice, archiveDevice, restoreDevice, deleteDevice } from "@/lib/api";
import type { Device } from "@/types";

export default function DeviceCard({
  device, employees, canAssign = false, canArchive = false, canDelete = false,
  onAssigned, onArchived, onRestored, onDeleted,
}: {
  device: Device;
  employees?: { id: string; name: string }[];
  canAssign?: boolean;
  canArchive?: boolean;
  canDelete?: boolean;
  onAssigned?: () => void;
  onArchived?: () => void;
  onRestored?: () => void;
  onDeleted?: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const lastSeen = device.last_seen_unix
    ? formatDistanceToNow(new Date(device.last_seen_unix * 1000), { addSuffix: true })
    : "never";

  async function handleAssign(employeeId: string) {
    setSaving(true);
    try {
      await assignDevice(device.id, employeeId || null);
      onAssigned?.();
    } catch (e) {
      alert(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  }

  async function handleArchive() {
    setSaving(true);
    try {
      await archiveDevice(device.id);
      onArchived?.();
    } catch (e) {
      alert(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  }

  async function handleRestore() {
    setSaving(true);
    try {
      await restoreDevice(device.id);
      onRestored?.();
    } catch (e) {
      alert(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!confirm(`Permanently delete "${device.hostname}"? The device will disappear from the UI. If the agent is still running it can re-register.`)) return;
    setSaving(true);
    try {
      await deleteDevice(device.id);
      onDeleted?.();
    } catch (e) {
      alert(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  }

  const showActions = canArchive || canDelete;

  return (
    <div className={`bg-white border rounded-lg p-3 hover:shadow-sm transition ${
      device.is_archived ? "border-amber-200 bg-amber-50/30" : "border-gray-200 hover:border-brand-300"
    }`}>
      <Link href={`/devices/${device.id}`} className="block cursor-pointer">
        <div className="flex items-start justify-between mb-2">
          <div className="min-w-0 flex-1 mr-2">
            <p className="font-medium text-xs text-gray-900 truncate">{device.hostname}</p>
            <p className="text-gray-600 text-[11px] mt-0.5 truncate">{device.employee_name ?? "Unassigned"}</p>
          </div>
          <div className="flex flex-col items-end gap-1 shrink-0">
            {device.is_archived ? (
              <span className="flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-amber-50 text-amber-600 border border-amber-200">
                <Archive size={9} />
                Archived
              </span>
            ) : (
              <span className={`flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full ${
                device.is_online
                  ? "bg-green-50 text-green-600 border border-green-200"
                  : "bg-gray-100 text-gray-600 border border-gray-200"
              }`}>
                <span className="relative flex items-center justify-center w-1.5 h-1.5">
                  {device.is_online && (
                    <span className="absolute inline-flex w-full h-full rounded-full bg-green-400 opacity-75 animate-ping" />
                  )}
                  <span className={`relative rounded-full w-1.5 h-1.5 ${device.is_online ? "bg-green-500" : "bg-gray-400"}`} />
                </span>
                {device.is_online ? "Online" : "Offline"}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center justify-between text-[11px] text-gray-600">
          <div className="flex items-center gap-1">
            <Clock size={10} />
            <span>{device.is_online ? "Active now" : `Seen ${lastSeen}`}</span>
          </div>
          <span className="capitalize text-gray-500">{device.platform}</span>
        </div>
      </Link>

      {canAssign && employees && !device.is_archived && (
        <div className="flex items-center gap-1 mt-2 pt-2 border-t border-gray-100">
          <UserRound size={11} className="text-gray-600 shrink-0" />
          <select
            value={device.employee_id ?? ""}
            disabled={saving}
            onChange={e => handleAssign(e.target.value)}
            className="w-full text-[11px] border border-gray-200 rounded px-1.5 py-1 text-gray-700 bg-white focus:outline-none focus:ring-1 focus:ring-brand-300 disabled:opacity-50"
            aria-label="Assign device to user"
          >
            <option value="">Unassigned</option>
            {employees.map(emp => (
              <option key={emp.id} value={emp.id}>{emp.name}</option>
            ))}
          </select>
        </div>
      )}

      {showActions && (
        <div className="flex items-center gap-1 mt-2 pt-2 border-t border-gray-100">
          {canArchive && !device.is_archived && (
            <button
              onClick={handleArchive}
              disabled={saving}
              title="Archive device"
              className="flex items-center gap-1 text-[11px] text-amber-600 hover:text-amber-700 hover:bg-amber-50 px-2 py-1 rounded transition disabled:opacity-40"
            >
              <Archive size={11} />
              Archive
            </button>
          )}
          {canArchive && device.is_archived && (
            <button
              onClick={handleRestore}
              disabled={saving}
              title="Restore device"
              className="flex items-center gap-1 text-[11px] text-green-600 hover:text-green-700 hover:bg-green-50 px-2 py-1 rounded transition disabled:opacity-40"
            >
              <RotateCcw size={11} />
              Restore
            </button>
          )}
          {canDelete && (
            <button
              onClick={handleDelete}
              disabled={saving}
              title="Delete device"
              className="flex items-center gap-1 text-[11px] text-red-500 hover:text-red-600 hover:bg-red-50 px-2 py-1 rounded transition disabled:opacity-40 ml-auto"
            >
              <Trash2 size={11} />
              Delete
            </button>
          )}
        </div>
      )}
    </div>
  );
}
