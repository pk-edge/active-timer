"use client";
import {
  PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from "recharts";
import { format, parseISO } from "date-fns";
import { Lock } from "lucide-react";
import { fmtSeconds } from "@/lib/api";

export function AccessDenied({ label = "this page" }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center">
      <div className="w-12 h-12 rounded-full bg-gray-100 flex items-center justify-center mb-3">
        <Lock size={20} className="text-gray-400" />
      </div>
      <p className="text-gray-700 font-medium">No access</p>
      <p className="text-gray-400 text-sm mt-1">You don&apos;t have permission to view {label}.</p>
    </div>
  );
}

const STATUS_COLORS: Record<string, string> = {
  active: "#22c55e", passive: "#f59e0b", inactive: "#cbd5e1",
};

export function StatusDonut({
  active, passive, inactive,
}: { active: number; passive: number; inactive: number }) {
  const total = active + passive + inactive;
  const data = [
    { name: "Active",   value: active },
    { name: "Passive",  value: passive },
    { name: "Inactive", value: inactive },
  ];
  const pctActive = total ? Math.round((active / total) * 100) : 0;
  return (
    <div className="relative">
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie data={data} dataKey="value" nameKey="name" cx="50%" cy="50%"
               innerRadius={64} outerRadius={92} paddingAngle={2} stroke="none">
            {data.map(d => <Cell key={d.name} fill={STATUS_COLORS[d.name.toLowerCase()]} />)}
          </Pie>
          <Tooltip contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }} />
        </PieChart>
      </ResponsiveContainer>
      <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
        <span className="text-3xl font-bold text-gray-900">{pctActive}%</span>
        <span className="text-xs text-gray-400">active now</span>
      </div>
    </div>
  );
}

export function ActivityTrend({ data }: { data: { date: string; active_seconds: number }[] }) {
  if (!data.length) {
    return <div className="h-[180px] flex items-center justify-center text-sm text-gray-400">No activity in this period.</div>;
  }
  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={data} barCategoryGap="30%">
        <XAxis dataKey="date" tickFormatter={d => { try { return format(parseISO(d), "EEE"); } catch { return d; } }}
               tick={{ fill: "#9ca3af", fontSize: 11 }} axisLine={false} tickLine={false} />
        <YAxis tickFormatter={v => `${Math.floor(v / 3600)}h`} tick={{ fill: "#9ca3af", fontSize: 11 }}
               axisLine={false} tickLine={false} width={30} />
        <Tooltip formatter={(v: number) => [fmtSeconds(v), "Active"]}
                 contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }} />
        <Bar dataKey="active_seconds" fill="#4a0f70" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

const PIE_COLORS = ["#4a0f70", "#22c55e", "#f59e0b", "#a855f7", "#ef4444", "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#6366f1", "#14b8a6", "#eab308"];

export function UsagePie({
  items, labelKey, valueKey, format: fmt,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
}: { items: any[]; labelKey: string; valueKey: string; format?: (v: number) => string }) {
  if (!items.length) {
    return <div className="h-[240px] flex items-center justify-center text-sm text-gray-400">No data yet.</div>;
  }
  const data = items.map(it => ({ name: String(it[labelKey]), value: Number(it[valueKey]) }));
  return (
    <div className="flex items-center gap-4">
      <ResponsiveContainer width="55%" height={240}>
        <PieChart>
          <Pie data={data} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={92} stroke="none">
            {data.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
          </Pie>
          <Tooltip formatter={(v: number) => (fmt ? fmt(v) : String(v))}
                   contentStyle={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, fontSize: 12 }} />
        </PieChart>
      </ResponsiveContainer>
      <div className="flex-1 space-y-1.5 min-w-0">
        {data.slice(0, 8).map((d, i) => (
          <div key={d.name} className="flex items-center gap-2 text-xs">
            <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: PIE_COLORS[i % PIE_COLORS.length] }} />
            <span className="text-gray-600 truncate flex-1">{d.name}</span>
            <span className="text-gray-400 shrink-0">{fmt ? fmt(d.value) : d.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
