"use client";
import { useEffect, useId, useRef, useState } from "react";
import { format, parseISO } from "date-fns";
import { AlertCircle, Download, FileSpreadsheet, FileText, Files, X } from "lucide-react";
import DateRangePicker, { type DateRange } from "@/components/DateRangePicker";
import { useStartReportJob } from "@/hooks/useStartReportJob";
import type { ReportFormat } from "@/contexts/ReportJobsContext";
import {
  defaultReportRange, describeReportRange, normalizeReportRange, reportRangeDays, validateReportRange,
} from "@/lib/reportRange";

export type ExportReportTarget =
  | { kind: "device"; deviceId: string; hostname: string; employeeName?: string | null }
  | { kind: "fleet" };

interface Props {
  target: ExportReportTarget;
  /** Pre-filled period. Defaults to Last 30 Days. Edits stay inside the dialog. */
  initialRange?: DateRange;
  onClose: () => void;
}

const FORMAT_OPTIONS: { value: ReportFormat; label: string; icon: React.ElementType }[] = [
  { value: "pdf",  label: "PDF",   icon: FileText },
  { value: "xlsx", label: "Excel", icon: FileSpreadsheet },
  { value: "both", label: "Both",  icon: Files },
];

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

const fmtDay = (iso: string) => {
  try { return format(parseISO(iso), "d MMM yyyy"); } catch { return iso; }
};

/**
 * Modal that exports a report for a chosen period. Device target runs
 * device_full for that device; fleet target runs the fleet report over the
 * devices the caller's role can see (the server scopes it). Closes before the
 * request resolves — progress lives in the Downloads tray.
 */
export default function ExportReportDialog({ target, initialRange, onClose }: Props) {
  const startReportJob = useStartReportJob();
  const [range, setRange] = useState<DateRange>(
    () => initialRange ? normalizeReportRange(initialRange) : defaultReportRange(),
  );
  const [fmt, setFmt] = useState<ReportFormat>("pdf");

  const uid = useId();
  const titleId = `${uid}-title`;
  const scopeId = `${uid}-scope`;
  const periodId = `${uid}-period`;
  const formatId = `${uid}-format`;
  const errorId = `${uid}-error`;

  const cardRef = useRef<HTMLDivElement>(null);
  // A backdrop click needs its mousedown AND click on the backdrop: mousedown
  // alone also fires on the overlay's own scrollbar.
  const downOnBackdrop = useRef(false);
  // Always call the latest onClose from the document listener without re-binding it.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Focus in on open; back to the trigger on close. Escape closes (an open period popover takes it first); Tab is trapped.
  useEffect(() => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cardRef.current?.focus();

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") { e.preventDefault(); onCloseRef.current(); return; }
      if (e.key !== "Tab" || !cardRef.current) return;
      const items = Array.from(cardRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      const outside = !cardRef.current.contains(active);
      if (e.shiftKey && (active === first || active === cardRef.current || outside)) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && (active === last || outside)) {
        e.preventDefault(); first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (trigger?.isConnected) trigger.focus();
    };
  }, []);

  const error = validateReportRange(range);
  const days = reportRangeDays(range);
  const isDevice = target.kind === "device";

  function generate() {
    if (error) return;
    if (target.kind === "device") {
      startReportJob({
        type: "device_full",
        format: fmt,
        label: `Device: ${target.hostname} · ${describeReportRange(range)}`,
        deviceId: target.deviceId,
        since: range.since,
        until: range.until,
      });
    } else {
      startReportJob({
        type: "fleet",
        format: fmt,
        label: `Fleet · ${describeReportRange(range)}`,
        since: range.since,
        until: range.until,
      });
    }
    onClose();
  }

  return (
    <div
      className="fixed inset-0 z-50 overflow-y-auto bg-black/40 backdrop-blur-sm"
      onMouseDown={e => {
        const el = e.currentTarget;
        downOnBackdrop.current =
          e.target === el && e.clientX < el.clientWidth && e.clientY < el.clientHeight;
      }}
      onClick={e => {
        if (e.target === e.currentTarget && downOnBackdrop.current) onClose();
        downOnBackdrop.current = false;
      }}
    >
      <div className="min-h-full flex items-center justify-center p-4 pointer-events-none">
        <div
          ref={cardRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          aria-describedby={scopeId}
          tabIndex={-1}
          className="pointer-events-auto bg-white rounded-2xl shadow-2xl p-4 sm:p-6 w-full max-w-md focus:outline-none"
        >
          {/* Header */}
          <div className="flex items-start justify-between gap-3 mb-4">
            <div className="min-w-0">
              <h2 id={titleId} className="text-base font-semibold text-gray-900">
                {isDevice ? "Export device report" : "Export fleet report"}
              </h2>
              <p className="text-xs text-gray-500 mt-0.5">Progress shows in the sidebar while you keep working.</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="shrink-0 -mt-1 -mr-1 p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300"
            >
              <X size={18} />
            </button>
          </div>

          {/* Report scope — exactly what will be exported */}
          <div id={scopeId} className="rounded-xl border border-gray-200 bg-gray-50 px-3.5 py-3 mb-5">
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Report scope</p>
            {target.kind === "device" ? (
              <p className="text-sm font-medium text-gray-900 break-words">
                {target.employeeName ?? "Unassigned"}
                <span className="font-normal text-gray-500"> · {target.hostname}</span>
              </p>
            ) : (
              <p className="text-sm font-medium text-gray-900">All devices you can see</p>
            )}
            <p className="text-xs text-gray-600 mt-0.5">
              {error
                ? "Choose a valid period below"
                : `${fmtDay(range.since)} → ${fmtDay(range.until)} · ${days} ${days === 1 ? "day" : "days"}`}
            </p>
          </div>

          {/* Period */}
          <div role="group" aria-labelledby={periodId} className="mb-5">
            <span id={periodId} className="block text-xs font-medium text-gray-600 mb-1.5">Period</span>
            <DateRangePicker value={range} onChange={r => setRange(normalizeReportRange(r))} />
            {error && (
              <p id={errorId} role="alert" className="flex items-start gap-1.5 mt-2 text-xs text-red-600">
                <AlertCircle size={13} className="shrink-0 mt-px" />
                <span>{error}</span>
              </p>
            )}
          </div>

          {/* Format */}
          <fieldset className="mb-6 min-w-0">
            <legend id={formatId} className="text-xs font-medium text-gray-600 mb-1.5">Format</legend>
            <div role="radiogroup" aria-labelledby={formatId} className="grid grid-cols-3 gap-2">
              {FORMAT_OPTIONS.map(opt => (
                <label
                  key={opt.value}
                  className={`relative flex flex-col items-center gap-1 py-2.5 rounded-xl border text-xs font-medium cursor-pointer transition has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-brand-300 ${
                    fmt === opt.value
                      ? "border-brand-400 bg-brand-50 text-brand-700"
                      : "border-gray-200 text-gray-600 hover:border-gray-300 hover:bg-gray-50"
                  }`}
                >
                  <input
                    type="radio"
                    name={`${uid}-format`}
                    value={opt.value}
                    checked={fmt === opt.value}
                    onChange={() => setFmt(opt.value)}
                    className="sr-only"
                  />
                  <opt.icon size={16} aria-hidden="true" />
                  {opt.label}
                </label>
              ))}
            </div>
          </fieldset>

          {/* Actions */}
          <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 border border-gray-200 rounded-lg hover:bg-gray-50 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={generate}
              disabled={!!error}
              aria-describedby={error ? errorId : undefined}
              className="flex items-center justify-center gap-1.5 px-4 py-2 text-sm font-medium text-white bg-brand-600 hover:bg-brand-700 rounded-lg transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300 focus-visible:ring-offset-1 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-brand-600"
            >
              <Download size={14} />
              Generate &amp; download
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
