"use client";
/**
 * Shared two-panel shell for Website Usage, Application Usage, Category Usage.
 * Left: filterable list table. Right: detail panel with tabs.
 */
import { useState, useMemo, useRef, useEffect } from "react";
import useSWR from "swr";
import {
  Search, RefreshCw, Download, ChevronDown, X,
  CheckCircle2, MinusCircle, Circle, BarChart2,
} from "lucide-react";
import { getUsers, fmtSeconds } from "@/lib/api";
import GroupPicker from "@/components/GroupPicker";
import DateRangePicker, { type DateRange } from "@/components/DateRangePicker";
import type { User } from "@/types";

export type { DateRange };

// ── Helpers ───────────────────────────────────────────────────────────────────

export const DATE_OPTIONS = [
  { label: "Today",      days: 1  },
  { label: "Last 7 days", days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "Last 60 days", days: 60 },
  { label: "Last 90 days", days: 90 },
];

export const PROD_OPTIONS = [
  { label: "All Classifications", value: "" },
  { label: "Productive",          value: "productive" },
  { label: "Unproductive",        value: "unproductive" },
  { label: "Neutral",             value: "neutral" },
];

export function fmtDur(secs: number): string {
  if (!secs) return "0s";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function ProdIcon({ v }: { v: string }) {
  if (v === "productive")   return <CheckCircle2 size={15} className="text-green-500 shrink-0" />;
  if (v === "unproductive") return <MinusCircle  size={15} className="text-red-400  shrink-0" />;
  return <Circle size={15} className="text-gray-500 shrink-0" />;
}

// ── Smart search bar ──────────────────────────────────────────────────────────

interface SearchBarProps {
  value:       string;
  onChange:    (v: string) => void;
  placeholder: string;
}

export function SmartSearchBar({ value, onChange, placeholder }: SearchBarProps) {
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "/" && document.activeElement?.tagName !== "INPUT") {
        e.preventDefault();
        ref.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="relative flex-1">
      <Search size={15} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
      <input
        ref={ref}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full pl-11 pr-10 py-2.5 bg-white border border-gray-200 rounded-xl text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 shadow-sm"
      />
      {value && (
        <button onClick={() => onChange("")}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-600">
          <X size={14} />
        </button>
      )}
      {!value && (
        <kbd className="absolute right-3 top-1/2 -translate-y-1/2 text-[10px] text-gray-500 bg-gray-100 px-1.5 py-0.5 rounded border border-gray-200 font-mono">
          /
        </kbd>
      )}
    </div>
  );
}

// ── Filter bar ────────────────────────────────────────────────────────────────

interface FilterBarProps {
  dateRange:       DateRange;
  setDateRange:    (r: DateRange) => void;
  groupId:         string;
  setGroupId:      (v: string) => void;
  employeeId:      string;
  setEmployeeId:   (v: string) => void;
  productivity?:   string;
  setProductivity?: (v: string) => void;
  showProdFilter?: boolean;
  summaryMode:     boolean;
  setSummaryMode:  (v: boolean) => void;
}

export function FilterBar({
  dateRange, setDateRange, groupId, setGroupId,
  employeeId, setEmployeeId,
  productivity = "", setProductivity,
  showProdFilter = true,
  summaryMode, setSummaryMode,
}: FilterBarProps) {
  const { data: users }  = useSWR<User[]>("users-filter", () => getUsers());

  return (
    <div className="flex flex-wrap items-center gap-2 mb-4">
      {/* Summary / Detailed toggle */}
      <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden shrink-0">
        {["Summary", "Detailed"].map(lbl => (
          <button key={lbl}
                  onClick={() => setSummaryMode(lbl === "Summary")}
                  className={`px-4 py-1.5 text-sm font-medium transition ${
                    (lbl === "Summary") === summaryMode
                      ? "bg-white text-gray-900 shadow-sm"
                      : "text-gray-600 hover:text-gray-700"
                  }`}>
            {lbl}
          </button>
        ))}
      </div>

      {/* Date range picker */}
      <DateRangePicker value={dateRange} onChange={setDateRange} />

      {/* Team / Group — includes subgroups in filter */}
      <GroupPicker
        value={groupId}
        onChange={v => { setGroupId(v); setEmployeeId(""); }}
      />

      {/* User */}
      <div className="relative shrink-0">
        <select value={employeeId} onChange={e => { setEmployeeId(e.target.value); setGroupId(""); }}
                className="appearance-none bg-white border border-gray-200 rounded-lg pl-3 pr-8 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 shadow-sm">
          <option value="">All Users</option>
          {(users ?? []).filter(u => u.is_active).map(u => (
            <option key={u.id} value={u.id}>{u.name}</option>
          ))}
        </select>
        <ChevronDown size={12} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
      </div>

      {/* Productivity */}
      {showProdFilter && setProductivity && (
        <div className="relative shrink-0">
          <select value={productivity} onChange={e => setProductivity(e.target.value)}
                  className="appearance-none bg-white border border-gray-200 rounded-lg pl-3 pr-8 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 shadow-sm">
            {PROD_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <ChevronDown size={12} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
        </div>
      )}
    </div>
  );
}

// ── Quick Stats bar ───────────────────────────────────────────────────────────

export function QuickStats({ stats }: { stats: { label: string; value: string; color?: string }[] }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm mb-4 overflow-hidden">
      <button onClick={() => setOpen(x => !x)}
              className="w-full flex items-center justify-between px-5 py-3 hover:bg-gray-50 transition">
        <p className="text-sm font-semibold text-gray-700 flex items-center gap-2">
          <BarChart2 size={15} className="text-gray-600" /> Quick Stats
        </p>
        <ChevronDown size={14} className={`text-gray-600 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="px-5 pb-4 pt-1 grid grid-cols-2 sm:grid-cols-4 gap-4 border-t border-gray-100">
          {stats.map(s => (
            <div key={s.label}>
              <p className={`text-xl font-bold leading-none ${s.color ?? "text-gray-900"}`}>{s.value}</p>
              <p className="text-xs text-gray-600 mt-1">{s.label}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Detail tab bar ────────────────────────────────────────────────────────────

export function DetailTabs({
  tabs, active, onSelect,
}: { tabs: string[]; active: string; onSelect: (t: string) => void }) {
  return (
    <div className="flex border-b border-gray-200 mb-4">
      {tabs.map(t => (
        <button key={t} onClick={() => onSelect(t)}
                className={`px-4 py-2.5 text-sm font-medium transition border-b-2 -mb-px ${
                  active === t
                    ? "border-brand-600 text-brand-600"
                    : "border-transparent text-gray-600 hover:text-gray-700"
                }`}>
          {t}
        </button>
      ))}
    </div>
  );
}

// ── Duration bar row ─────────────────────────────────────────────────────────

export function DurationRow({
  label, duration_secs, pct, color = "#4a0f70", sub,
}: { label: string; duration_secs: number; pct: number; color?: string; sub?: string }) {
  return (
    <div className="py-2.5 border-b border-gray-100 last:border-0">
      <div className="flex items-center justify-between mb-1 gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-sm text-gray-800 truncate" title={label}>{label}</p>
          {sub && <p className="text-xs text-gray-600 truncate">{sub}</p>}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <span className="text-sm text-gray-700 font-medium tabular-nums">{fmtDur(duration_secs)}</span>
          <span className="text-xs text-gray-600 w-8 text-right">{pct.toFixed(1)}%</span>
        </div>
      </div>
      <div className="h-1 bg-gray-100 rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(pct, 100)}%`, background: color }} />
      </div>
    </div>
  );
}

// ── Export CSV ────────────────────────────────────────────────────────────────

export function exportCsv(headers: string[], rows: string[][], filename: string) {
  const csv = [headers, ...rows].map(r =>
    r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(",")
  ).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}
