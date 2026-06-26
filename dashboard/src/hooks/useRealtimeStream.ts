"use client";
import { useState, useEffect } from "react";
import { BASE } from "@/lib/api";
import type { RealtimeDevice } from "@/types";

/**
 * Connects to the /v1/home/stream SSE endpoint and returns live device
 * status. Replaces HTTP polling — one persistent connection instead of a
 * recurring request every N seconds. Reconnects automatically with
 * exponential back-off (1 s → 30 s) on network errors.
 */
export function useRealtimeStream(groupId?: string): RealtimeDevice[] {
  const [devices, setDevices] = useState<RealtimeDevice[]>([]);

  useEffect(() => {
    const token =
      typeof window !== "undefined" ? localStorage.getItem("tt_token") : null;
    if (!token) return;

    const ctrl = new AbortController();
    let backoff = 1_000;

    async function connect() {
      while (!ctrl.signal.aborted) {
        try {
          const qs = groupId ? `?group_id=${groupId}` : "";
          const res = await fetch(`${BASE}/v1/home/stream${qs}`, {
            headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
            signal: ctrl.signal,
            cache: "no-store",
          });

          if (!res.ok || !res.body) {
            await new Promise(r => setTimeout(r, backoff));
            backoff = Math.min(backoff * 2, 30_000);
            continue;
          }

          backoff = 1_000;
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";

          while (!ctrl.signal.aborted) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split("\n");
            buf = lines.pop() ?? "";
            for (const line of lines) {
              if (line.startsWith("data: ")) {
                try {
                  setDevices(JSON.parse(line.slice(6)));
                } catch { /* skip malformed frame */ }
              }
            }
          }
        } catch {
          if (ctrl.signal.aborted) break;
          await new Promise(r => setTimeout(r, backoff));
          backoff = Math.min(backoff * 2, 30_000);
        }
      }
    }

    connect();
    return () => ctrl.abort();
  }, [groupId]);

  return devices;
}
