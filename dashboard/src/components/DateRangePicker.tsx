"use client";
import { useState } from "react";
import { format, subDays, startOfMonth, endOfMonth, startOfYear, subMonths, subYears } from "date-fns";
import { CalendarDays, ChevronDown } from "lucide-react";

export interface DateRange { since: string; until: string; }

const today = () => format(new Date(), "yyyy-MM-dd");
const d     = (n: number) => format(subDays(new Date(), n), "yyyy-MM-dd");

const PRESETS = [
  { label: "Today",          get: (): DateRange => { const t = today(); return { since: t, until: t }; } },
  { label: "Yesterday",      get: (): DateRange => { const y = d(1); return { since: y, until: y }; } },
  { label: "Last 7 Days",    get: (): DateRange => ({ since: d(6),   until: today() }) },
  { label: "Last 30 Days",   get: (): DateRange => ({ since: d(29),  until: today() }) },
  { label: "Last 90 Days",   get: (): DateRange => ({ since: d(89),  until: today() }) },
  { label: "This Month",     get: (): DateRange => ({ since: format(startOfMonth(new Date()), "yyyy-MM-dd"), until: today() }) },
  { label: "Last Month",     get: (): DateRange => {
      const m = subMonths(new Date(), 1);
      return { since: format(startOfMonth(m), "yyyy-MM-dd"), until: format(endOfMonth(m), "yyyy-MM-dd") };
    }
  },
  { label: "Last 6 Months",  get: (): DateRange => ({ since: format(subMonths(new Date(), 6), "yyyy-MM-dd"), until: today() }) },
  { label: "Last Year",      get: (): DateRange => ({ since: d(364), until: today() }) },
  { label: "This Year",      get: (): DateRange => ({ since: format(startOfYear(new Date()), "yyyy-MM-dd"), until: today() }) },
  { label: "Last 2 Years",   get: (): DateRange => ({ since: format(subYears(new Date(), 2), "yyyy-MM-dd"), until: today() }) },
];

interface Props {
  value:    DateRange;
  onChange: (r: DateRange) => void;
}

function detectPreset(range: DateRange): string {
  for (const p of PRESETS) {
    const g = p.get();
    if (g.since === range.since && g.until === range.until) return p.label;
  }
  return "Custom";
}

export default function DateRangePicker({ value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const preset = detectPreset(value);

  function applyPreset(p: typeof PRESETS[number]) {
    onChange(p.get());
    setOpen(false);
  }

  return (
    <div className="relative">
      {/* Trigger */}
      <button
        onClick={() => setOpen(x => !x)}
        className="flex items-center gap-2 bg-white border border-gray-200 rounded-lg px-3 py-1.5 text-sm text-gray-700 hover:border-blue-300 shadow-sm transition"
      >
        <CalendarDays size={14} className="text-gray-400 shrink-0" />
        <span className="font-medium">{preset}</span>
        <span className="text-gray-400 text-xs">
          {value.since === value.until ? value.since : `${value.since} – ${value.until}`}
        </span>
        <ChevronDown size={12} className={`text-gray-400 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {/* Dropdown */}
      {open && (
        <>
          {/* Backdrop */}
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-full mt-1.5 z-40 bg-white border border-gray-200 rounded-xl shadow-xl w-80">
            {/* Preset grid */}
            <div className="p-3 border-b border-gray-100">
              <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Quick Select</p>
              <div className="grid grid-cols-2 gap-1">
                {PRESETS.map(p => (
                  <button
                    key={p.label}
                    onClick={() => applyPreset(p)}
                    className={`text-left px-3 py-1.5 rounded-lg text-sm transition ${
                      preset === p.label
                        ? "bg-blue-50 text-blue-700 font-medium"
                        : "text-gray-600 hover:bg-gray-50"
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Custom date inputs */}
            <div className="p-3">
              <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Custom Range</p>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="block text-xs text-gray-500 mb-1">From</label>
                  <input
                    type="date"
                    value={value.since}
                    max={value.until}
                    onChange={e => onChange({ ...value, since: e.target.value })}
                    className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-300"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">To</label>
                  <input
                    type="date"
                    value={value.until}
                    min={value.since}
                    max={today()}
                    onChange={e => onChange({ ...value, until: e.target.value })}
                    className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-300"
                  />
                </div>
              </div>
              <button
                onClick={() => setOpen(false)}
                className="mt-2 w-full bg-blue-600 text-white text-sm py-1.5 rounded-lg hover:bg-blue-700 transition font-medium"
              >
                Apply
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
