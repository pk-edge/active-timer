"use client";
/**
 * Per-day "active time" bar chart for the Reports page.
 *
 * Split into its own module so the heavy `recharts` dependency is loaded lazily
 * (via next/dynamic at the call site) instead of in the Reports route's initial
 * bundle — the page shell and filters paint immediately, the chart streams in.
 */
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { fmtSeconds } from "@/lib/api";
import type { CompanyDay } from "@/types";

export default function DailyActiveBarChart({ data }: { data: CompanyDay[] }) {
  return (
    <div className="h-32 sm:h-40">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={[...data].reverse()} barCategoryGap="35%">
          <XAxis
            dataKey="date"
            tick={{ fill: "#9ca3af", fontSize: 10 }}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            tickFormatter={v => `${Math.floor(v / 3600)}h`}
            tick={{ fill: "#9ca3af", fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            width={26}
          />
          <Tooltip
            formatter={(v: number) => [fmtSeconds(v), "Active"]}
            contentStyle={{
              background: "#fff",
              border: "1px solid #e5e7eb",
              borderRadius: 8,
              fontSize: 12,
              color: "#111827",
            }}
          />
          <Bar dataKey="active_seconds" fill="#4a0f70" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
