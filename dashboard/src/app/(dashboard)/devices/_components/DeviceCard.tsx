"use client";
import Link from "next/link";
import { formatDistanceToNow } from "date-fns";
import { Clock } from "lucide-react";
import type { Device } from "@/types";

export default function DeviceCard({ device }: { device: Device }) {
  const lastSeen = device.last_seen_unix
    ? formatDistanceToNow(new Date(device.last_seen_unix * 1000), { addSuffix: true })
    : "never";

  return (
    <Link href={`/devices/${device.id}`}>
      <div className="bg-white border border-gray-200 rounded-xl p-4 hover:border-blue-300 hover:shadow-sm transition cursor-pointer">
        <div className="flex items-start justify-between mb-3">
          <div>
            <p className="font-medium text-sm text-gray-900">{device.hostname}</p>
            <p className="text-gray-500 text-xs mt-0.5">{device.employee_name ?? "Unassigned"}</p>
          </div>
          <span
            className={`flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-full ${
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
        <div className="flex items-center gap-1.5 text-gray-400 text-xs">
          <Clock size={12} />
          <span>{device.is_online ? "Active now" : `Last seen ${lastSeen}`}</span>
        </div>
        <p className="text-gray-400 text-xs mt-1 capitalize">{device.platform}</p>
      </div>
    </Link>
  );
}
