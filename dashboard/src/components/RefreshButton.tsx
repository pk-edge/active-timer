"use client";
import { useState, useCallback } from "react";
import { RefreshCw } from "lucide-react";

interface RefreshButtonProps {
  /** Async refresh action — typically one or more SWR mutate() calls. */
  onRefresh: () => Promise<unknown> | void;
  label?: string;
  className?: string;
}

/**
 * Refresh button with real feedback:
 *  - spins the icon and disables itself while the refresh promise is pending
 *  - guarantees a minimum spin time so a fast refetch is still visible
 *  - shows a brief error title if the refresh throws
 */
export default function RefreshButton({
  onRefresh,
  label = "Refresh",
  className = "",
}: RefreshButtonProps) {
  const [spinning, setSpinning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handle = useCallback(async () => {
    if (spinning) return;
    setSpinning(true);
    setError(null);
    const started = Date.now();
    try {
      await Promise.resolve(onRefresh());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Refresh failed");
    } finally {
      // Keep the spinner visible for at least 450ms so the click registers.
      const elapsed = Date.now() - started;
      if (elapsed < 450) await new Promise(r => setTimeout(r, 450 - elapsed));
      setSpinning(false);
    }
  }, [onRefresh, spinning]);

  return (
    <button
      onClick={handle}
      disabled={spinning}
      title={error ?? undefined}
      className={
        className ||
        "flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg border border-gray-200 bg-white shadow-sm transition disabled:opacity-70"
      }
    >
      <RefreshCw size={13} className={spinning ? "animate-spin" : ""} />
      {error ? "Retry" : label}
    </button>
  );
}
