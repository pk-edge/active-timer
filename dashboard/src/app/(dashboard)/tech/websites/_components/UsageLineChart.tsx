"use client";
/**
 * "Visits over time" line chart for a website's detail panel.
 *
 * Extracted so recharts is lazy-loaded (next/dynamic at the call site): the
 * chart only appears under the "Usage" tab of a selected website, so it should
 * not be part of the page's initial bundle.
 */
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

export default function UsageLineChart({
  data, color,
}: { data: { date: string; visits: number }[]; color: string }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data}>
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#9ca3af" }} axisLine={false} tickLine={false} />
        <YAxis tick={{ fontSize: 10, fill: "#9ca3af" }} axisLine={false} tickLine={false} width={30} />
        <Tooltip
          formatter={(v: number) => [v, "Visits"]}
          contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 11 }}
        />
        <Line type="monotone" dataKey="visits" stroke={color} strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
