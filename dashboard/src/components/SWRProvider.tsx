"use client";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";
import { AbortedError } from "@/lib/api";

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
          if (err instanceof AbortedError || (err as { aborted?: boolean })?.aborted) return;
        },
      }}
    >
      {children}
    </SWRConfig>
  );
}
