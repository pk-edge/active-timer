"use client";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import { AbortedError, HttpError, RateLimitedError, ServiceBusyError } from "@/lib/api";

/** A navigation-aborted request must never be retried or surfaced as an error. */
function isAbort(err: unknown): boolean {
  return (
    err instanceof AbortedError ||
    (err as { aborted?: boolean })?.aborted === true ||
    (err as { name?: string })?.name === "AbortError"
  );
}

// Longest we will wait on a server-provided Retry-After before the one automatic
// retry. Anything longer is left to the user's explicit Retry button.
const MAX_RETRY_AFTER_S = 60;

export default function SWRProvider({ children }: { children: ReactNode }) {
  return (
    <SWRConfig
      value={{
        revalidateOnFocus: false,
        revalidateOnReconnect: true,
        keepPreviousData: true,
        // Identical keys fired within this window share one request (a remount or
        // a second component using the same key does not re-hit the API) — keeps
        // normal navigation well under the server's 20-requests-per-5 s burst limit.
        dedupingInterval: 5_000,
        focusThrottleInterval: 30_000,
        errorRetryCount: 2,
        onError: (err) => {
          if (isAbort(err)) return;
        },
        // Stop SWR's default error-retry from re-issuing a request we aborted on
        // navigation — that re-fire was the "cancel then immediately re-send" bug.
        onErrorRetry: (err, _key, config, revalidate, opts) => {
          if (isAbort(err)) return;

          // Rate-limited / shed by the server: retry ONCE, after the server's own
          // Retry-After (capped). Retrying sooner just re-trips the limiter.
          if (err instanceof RateLimitedError || err instanceof ServiceBusyError) {
            if (opts.retryCount > 1 || err.retryAfterSeconds > MAX_RETRY_AFTER_S) return;
            err.autoRetryScheduled = true;
            // Small jitter so several cards that were limited together don't all
            // retry in the same millisecond.
            const waitMs = err.retryAfterSeconds * 1000 + Math.round(Math.random() * 500);
            setTimeout(() => revalidate(opts), waitMs);
            return;
          }

          // 4xx (validation, permission, not-found) is deterministic — retrying
          // can only repeat the same failure.
          if (err instanceof HttpError && err.status >= 400 && err.status < 500) return;
          if (err instanceof Error && err.message === "Unauthorized") return;

          if (opts.retryCount >= (config.errorRetryCount ?? 0)) return;
          setTimeout(() => revalidate(opts), config.errorRetryInterval ?? 5_000);
        },
      }}
    >
      {children}
    </SWRConfig>
  );
}
