"use client";
import { format, parseISO } from "date-fns";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import { fmtSeconds } from "@/lib/api";
import type { DailyStat } from "@/types";

interface Props {
  stats:   DailyStat[];
  selDate: string;
}

export default function DailyChart({ stats, selDate }: Props) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl px-4 pt-3 pb-2 shadow-sm shrink-0">
      <p className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-2">
        Active hours — last 7 days
      </p>
      <ResponsiveContainer width="100%" height={110}>
        <BarChart data={[...stats].reverse()} barCategoryGap="35%" margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
          <XAxis
            dataKey="date"
            tickFormatter={d => format(parseISO(d), "EEE")}
            tick={{ fill: "#9ca3af", fontSize: 10 }}
            axisLine={false} tickLine={false}
          />
          <YAxis
            tickFormatter={v => `${Math.floor(v / 3600)}h`}
            tick={{ fill: "#9ca3af", fontSize: 10 }}
            axisLine={false} tickLine={false} width={24}
          />
          <Tooltip
            formatter={(v: number) => [fmtSeconds(v), "Active"]}
            contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 11, color: "#111827" }}
          />
          <Bar dataKey="active_seconds" radius={[3, 3, 0, 0]}>
            {[...stats].reverse().map((s, i) => (
              <Cell key={i} fill={s.date === selDate ? "#4a0f70" : "#bfdbfe"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
