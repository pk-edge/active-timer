"use client";
import { useState } from "react";
import Link from "next/link";
import { formatDistanceToNow } from "date-fns";
import { Clock, UserRound } from "lucide-react";
import { assignDevice } from "@/lib/api";
import type { Device } from "@/types";

export default function DeviceCard({ device, employees, canAssign = false, onAssigned }: {
  device: Device;
  employees?: { id: string; name: string }[];
  canAssign?: boolean;
  onAssigned?: () => void;
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

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3 hover:border-brand-300 hover:shadow-sm transition">
      <Link href={`/devices/${device.id}`} className="block cursor-pointer">
        <div className="flex items-start justify-between mb-2">
          <div className="min-w-0 flex-1 mr-2">
            <p className="font-medium text-xs text-gray-900 truncate">{device.hostname}</p>
            <p className="text-gray-500 text-[11px] mt-0.5 truncate">{device.employee_name ?? "Unassigned"}</p>
          </div>
          <span
            className={`flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full shrink-0 ${
              device.is_online
                ? "bg-green-50 text-green-600 border border-green-200"
                : "bg-gray-100 text-gray-500 border border-gray-200"
            }`}
          >
            <span className="relative flex items-center justify-center w-1.5 h-1.5">
              {device.is_online && (
                <span className="absolute inline-flex w-full h-full rounded-full bg-green-400 opacity-75 animate-ping" />
              )}
              <span className={`relative rounded-full w-1.5 h-1.5 ${device.is_online ? "bg-green-500" : "bg-gray-400"}`} />
            </span>
            {device.is_online ? "Online" : "Offline"}
          </span>
        </div>
        <div className="flex items-center justify-between text-[11px] text-gray-400">
          <div className="flex items-center gap-1">
            <Clock size={10} />
            <span>{device.is_online ? "Active now" : `Seen ${lastSeen}`}</span>
          </div>
          <span className="capitalize text-gray-300">{device.platform}</span>
        </div>
      </Link>

      {canAssign && employees && (
        <div className="flex items-center gap-1 mt-2 pt-2 border-t border-gray-100">
          <UserRound size={11} className="text-gray-400 shrink-0" />
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
    </div>
  );
}
