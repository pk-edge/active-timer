"use client";
/**
 * "Usage over time" bar chart for an application's detail panel.
 *
 * Extracted so recharts is lazy-loaded (next/dynamic at the call site): the
 * chart only appears under the "Usage" tab of a selected app, so there is no
 * reason to ship recharts in the page's initial bundle.
 */
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { fmtDur } from "@/components/UsagePageShell";

export default function UsageBarChart({
  data, color,
}: { data: { date: string; duration_secs: number }[]; color: string }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} barCategoryGap="35%">
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#9ca3af" }} axisLine={false} tickLine={false} />
        <YAxis tickFormatter={v => `${Math.floor(v / 3600)}h`}
               tick={{ fontSize: 10, fill: "#9ca3af" }} axisLine={false} tickLine={false} width={28} />
        <Tooltip
          formatter={(v: number) => [fmtDur(v), "Duration"]}
          contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 11 }}
        />
        <Bar dataKey="duration_secs" fill={color} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
