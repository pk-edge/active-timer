"use client";
import { useEffect, useRef } from "react";
import { AlertTriangle, Trash2, X } from "lucide-react";

interface ConfirmDialogProps {
  open:          boolean;
  title:         string;
  message:       string;
  confirmLabel?: string;
  /** Extra detail shown in a muted block under the message */
  detail?:       string;
  /** "danger" = red confirm (default), "warning" = amber */
  variant?:      "danger" | "warning";
  loading?:      boolean;
  onConfirm:     () => void;
  onCancel:      () => void;
}

/**
 * Accessible confirmation dialog — replaces browser confirm().
 * Traps focus while open. Closes on Escape.
 */
export default function ConfirmDialog({
  open, title, message, detail,
  confirmLabel = "Delete",
  variant = "danger",
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onCancel]);

  // Focus cancel button when dialog opens
  useEffect(() => {
    if (open) setTimeout(() => cancelRef.current?.focus(), 50);
  }, [open]);

  if (!open) return null;

  const isDanger  = variant === "danger";
  const iconBg    = isDanger ? "bg-red-50"   : "bg-amber-50";
  const iconColor = isDanger ? "text-red-500" : "text-amber-500";
  const btnClass  = isDanger
    ? "bg-red-600 hover:bg-red-700 focus:ring-red-500"
    : "bg-amber-500 hover:bg-amber-600 focus:ring-amber-400";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onCancel(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
    >
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
        {/* Header */}
        <div className="flex items-start gap-4 p-6 pb-4">
          <div className={`w-10 h-10 rounded-full ${iconBg} flex items-center justify-center shrink-0`}>
            {isDanger
              ? <Trash2      size={18} className={iconColor} />
              : <AlertTriangle size={18} className={iconColor} />}
          </div>
          <div className="flex-1 min-w-0">
            <h2 id="confirm-title" className="text-base font-semibold text-gray-900 leading-snug">
              {title}
            </h2>
            <p className="text-sm text-gray-600 mt-1 leading-relaxed">{message}</p>
            {detail && (
              <p className="text-xs text-gray-400 mt-2 bg-gray-50 rounded-lg px-3 py-2 border border-gray-100">
                {detail}
              </p>
            )}
          </div>
          <button
            onClick={onCancel}
            className="text-gray-400 hover:text-gray-600 shrink-0 -mt-1"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Actions */}
        <div className="flex gap-3 px-6 py-4 bg-gray-50 border-t border-gray-100">
          <button
            ref={cancelRef}
            onClick={onCancel}
            disabled={loading}
            className="flex-1 px-4 py-2.5 text-sm font-medium text-gray-700 bg-white border border-gray-200 rounded-xl hover:bg-gray-50 transition focus:outline-none focus:ring-2 focus:ring-gray-300 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={`flex-1 px-4 py-2.5 text-sm font-medium text-white rounded-xl transition focus:outline-none focus:ring-2 focus:ring-offset-1 disabled:opacity-60 ${btnClass}`}
          >
            {loading ? "Deleting…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
