"use client";
import { useEffect } from "react";
import { useSWRConfig } from "swr";
import { BASE } from "@/lib/api";

// Reconnect backoff: 3 s, doubling per consecutive failure, capped at 60 s, with
// up to 50 % random jitter so a fleet of tabs does not reconnect in lockstep.
const RETRY_MIN_MS = 3_000;
const RETRY_MAX_MS = 60_000;

/**
 * Opens an SSE connection to /v1/devices/stream.
 * When the backend publishes "devices_updated" (on every agent sync),
 * this hook calls mutate("devices") so the devices page re-fetches instantly.
 *
 * Reconnects after any error with exponential backoff + jitter (EventSource
 * cannot read the HTTP status, so a 429 and an outage look the same). The
 * attempt counter resets once the stream opens or delivers a message. While the
 * tab is hidden the stream is closed and no retry is scheduled; it reconnects
 * immediately when the tab becomes visible again.
 */
export function useDevicesPush(showArchived = false) {
  const { mutate } = useSWRConfig();

  useEffect(() => {
    const token = localStorage.getItem("tt_token") ?? "";
    if (!token) return;

    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    let closed = false;

    const hidden = () => document.visibilityState === "hidden";

    function disconnect() {
      if (es) {
        es.close();
        es = null;
      }
    }

    function scheduleRetry() {
      if (closed || hidden()) return; // resumed by onVisibility
      const base = Math.min(RETRY_MAX_MS, RETRY_MIN_MS * 2 ** attempts);
      attempts += 1;
      const delay = Math.min(RETRY_MAX_MS, base + Math.random() * base * 0.5);
      clearTimeout(retryTimer);
      retryTimer = setTimeout(connect, delay);
    }

    function connect() {
      if (closed || hidden()) return;
      disconnect();
      const source = new EventSource(
        `${BASE}/v1/devices/stream?token=${encodeURIComponent(token)}`
      );
      es = source;

      source.onopen = () => {
        attempts = 0;
      };

      source.addEventListener("devices_updated", () => {
        attempts = 0;
        // Invalidate whichever SWR key the page is currently subscribed to.
        mutate(showArchived ? "devices?show_archived=true" : "devices");
      });

      source.onerror = () => {
        // Close ourselves so the browser's own (un-backed-off) retry never runs.
        source.close();
        if (es === source) es = null;
        scheduleRetry();
      };
    }

    function onVisibility() {
      if (closed) return;
      if (hidden()) {
        clearTimeout(retryTimer);
        disconnect();
      } else if (!es) {
        // Back in view: reconnect now (the data is stale anyway) and refetch.
        clearTimeout(retryTimer);
        connect();
        mutate(showArchived ? "devices?show_archived=true" : "devices");
      }
    }

    document.addEventListener("visibilitychange", onVisibility);
    connect();

    return () => {
      closed = true;
      clearTimeout(retryTimer);
      document.removeEventListener("visibilitychange", onVisibility);
      disconnect();
    };
  }, [mutate, showArchived]);
}
