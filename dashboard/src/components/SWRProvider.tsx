"use client";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import { AbortedError } from "@/lib/api";

/** A navigation-aborted request must never be retried or surfaced as an error. */
function isAbort(err: unknown): boolean {
  return (
    err instanceof AbortedError ||
    (err as { aborted?: boolean })?.aborted === true ||
    (err as { name?: string })?.name === "AbortError"
  );
}

export default function SWRProvider({ children }: { children: ReactNode }) {
  return (
    <SWRConfig
      value={{
        revalidateOnFocus: false,
        revalidateOnReconnect: true,
        keepPreviousData: true,
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
          if (opts.retryCount >= (config.errorRetryCount ?? 0)) return;
          setTimeout(() => revalidate(opts), config.errorRetryInterval ?? 5_000);
        },
      }}
    >
      {children}
    </SWRConfig>
  );
}
