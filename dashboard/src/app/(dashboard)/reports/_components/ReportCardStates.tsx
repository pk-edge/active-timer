"use client";
/**
 * Non-data states for the Reports page cards (stat cards / daily chart / top apps):
 * the inline error panel and the actionable empty state. Kept beside the page so
 * every card reports a failure or an empty range the same way — and, crucially, so
 * a failed or slow request never leaves the PREVIOUS range's numbers on screen
 * looking current.
 */
import { useEffect, useState } from "react";
import { AlertTriangle, CalendarRange, RefreshCw } from "lucide-react";
import { HttpError, RateLimitedError, ServiceBusyError } from "@/lib/api";

/** A navigation-aborted request is not a failure to show the user. */
export function isAbortError(err: unknown): boolean {
  return (
    (err as { aborted?: boolean })?.aborted === true ||
    (err as { name?: string })?.name === "AbortError"
  );
}

interface ErrorCopy {
  /** Short, human headline. */
  title: string;
  /** Extra sentence(s) under it, if any. */
  detail?: string;
  /** Seconds the visible countdown runs for (rate limit / busy), if any. */
  waitSeconds?: number;
  /** Whether SWR has queued an automatic retry after that wait. */
  autoRetry: boolean;
}

function describeLoadError(err: unknown, what: string): ErrorCopy {
  if (err instanceof RateLimitedError) {
    return {
      title: err.autoRetryScheduled
        ? "Too many requests"
        : `Too many requests — try again in ${err.retryAfterSeconds} s`,
      detail: `${what} will load as soon as the limit clears.`,
      waitSeconds: err.retryAfterSeconds,
      autoRetry: err.autoRetryScheduled,
    };
  }
  if (err instanceof ServiceBusyError) {
    return {
      title: `Couldn't load ${what}`,
      detail: err.message,
      waitSeconds: err.retryAfterSeconds,
      autoRetry: err.autoRetryScheduled,
    };
  }
  if (err instanceof HttpError && err.status >= 500) {
    return {
      title: `Couldn't load ${what}`,
      detail: "The server couldn't finish this request. Try again, or narrow the date range.",
      autoRetry: false,
    };
  }
  if (err instanceof TypeError) {
    return {
      title: `Couldn't load ${what}`,
      detail: "Network problem — check your connection and retry.",
      autoRetry: false,
    };
  }
  return {
    title: `Couldn't load ${what}`,
    detail: err instanceof Error ? err.message : undefined,
    autoRetry: false,
  };
}

/** Counts down once a second from `seconds`; restarts whenever `resetKey` changes. */
function useCountdown(seconds: number | undefined, resetKey: unknown): number {
  const [left, setLeft] = useState(seconds ?? 0);
  useEffect(() => {
    if (seconds === undefined) return;
    setLeft(seconds);
    const id = setInterval(() => setLeft(l => Math.max(0, l - 1)), 1000);
    return () => clearInterval(id);
  }, [seconds, resetKey]);
  return left;
}

/**
 * Inline error panel that REPLACES a card's data. `retrying` is SWR's isValidating
 * — true while the automatic or manual retry is in flight.
 */
export function ReportErrorPanel({
  error, what, onRetry, retrying, className = "",
}: {
  error: unknown;
  /** Lower-case noun phrase, e.g. "the daily chart". */
  what: string;
  onRetry: () => void;
  retrying: boolean;
  className?: string;
}) {
  const copy = describeLoadError(error, what);
  const left = useCountdown(copy.waitSeconds, error);
  const waiting = copy.autoRetry && left > 0 && !retrying;

  return (
    <div
      role="alert"
      className={`flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 ${className}`}
    >
      <AlertTriangle size={16} className="text-red-500 mt-0.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-red-700">{copy.title}</p>
        {copy.detail && <p className="text-xs text-red-600 mt-0.5 break-words">{copy.detail}</p>}
        {/* The ticking number is aria-hidden: an alert region that changes every
            second would be re-announced continuously. The static sentence is
            what assistive tech reads. */}
        {waiting && (
          <>
            <p className="text-xs text-red-600 mt-0.5" aria-hidden="true">Retrying in {left} s…</p>
            <span className="sr-only">Retrying automatically shortly.</span>
          </>
        )}
        {retrying && <p className="text-xs text-red-600 mt-0.5">Retrying…</p>}
      </div>
      <button
        type="button"
        onClick={onRetry}
        disabled={retrying}
        className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 bg-white text-red-700 hover:bg-red-100 shadow-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-red-300 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <RefreshCw size={13} className={retrying ? "animate-spin motion-reduce:animate-none" : ""} aria-hidden="true" />
        Retry
      </button>
    </div>
  );
}

/** Actionable empty state: says what is missing and offers the next step. */
export function ReportEmptyPanel({
  title = "No activity in this range",
  hint = "Try a wider date range, or pick a different device.",
  onWiden,
  widenLabel = "Show last 30 days",
  onClearDevice,
  className = "",
}: {
  title?: string;
  hint?: string;
  onWiden?: () => void;
  widenLabel?: string;
  onClearDevice?: () => void;
  className?: string;
}) {
  return (
    <div className={`flex flex-col items-center justify-center text-center gap-2 py-6 ${className}`}>
      <CalendarRange size={20} className="text-gray-400" aria-hidden="true" />
      <p className="text-sm font-medium text-gray-700">{title}</p>
      <p className="text-xs text-gray-600 max-w-sm">{hint}</p>
      {(onWiden || onClearDevice) && (
        <div className="flex flex-wrap items-center justify-center gap-2 mt-1">
          {onWiden && (
            <button
              type="button"
              onClick={onWiden}
              className="px-3 py-1.5 text-sm font-medium rounded-lg border border-brand-200 bg-brand-50 text-brand-700 hover:border-brand-300 hover:bg-brand-100 shadow-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300"
            >
              {widenLabel}
            </button>
          )}
          {onClearDevice && (
            <button
              type="button"
              onClick={onClearDevice}
              className="px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 shadow-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300"
            >
              Show all devices
            </button>
          )}
        </div>
      )}
    </div>
  );
}
