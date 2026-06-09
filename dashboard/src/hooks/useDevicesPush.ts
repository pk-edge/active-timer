"use client";
import { useEffect } from "react";
import { useSWRConfig } from "swr";
import { BASE } from "@/lib/api";

/**
 * Opens an SSE connection to /v1/devices/stream.
 * When the backend publishes "devices_updated" (on every agent sync),
 * this hook calls mutate("devices") so the devices page re-fetches instantly.
 * Reconnects automatically after any error.
 */
export function useDevicesPush() {
  const { mutate } = useSWRConfig();

  useEffect(() => {
    const token = localStorage.getItem("tt_token") ?? "";
    if (!token) return;

    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout>;
    let closed = false;

    function connect() {
      if (closed) return;
      es = new EventSource(
        `${BASE}/v1/devices/stream?token=${encodeURIComponent(token)}`
      );

      es.addEventListener("devices_updated", () => {
        mutate("devices");
      });

      es.onerror = () => {
        es?.close();
        if (!closed) {
          retryTimer = setTimeout(connect, 3000);
        }
      };
    }

    connect();

    return () => {
      closed = true;
      clearTimeout(retryTimer);
      es?.close();
    };
  }, [mutate]);
}
