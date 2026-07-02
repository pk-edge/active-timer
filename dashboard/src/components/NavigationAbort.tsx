"use client";
import { useLayoutEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { abortInFlightGets } from "@/lib/api";

/**
 * Kills the previous page's in-flight GET requests whenever the route changes,
 * so navigating away from a heavy page (logs, tech analytics) immediately
 * frees the browser connections AND lets the backend cancel the queries.
 * Renders nothing; mounted once in the dashboard layout.
 *
 * This runs in a layout effect (before paint, before passive effects) so the
 * controller is rolled over BEFORE the incoming page's data hooks fire their
 * GETs in their own (passive) effects. If it ran as a passive effect it could
 * race the new page — cancelling the request it just started, which then got
 * re-sent. The new page's requests attach to the fresh controller and survive.
 */
export default function NavigationAbort() {
  const pathname = usePathname();
  const prev = useRef(pathname);

  useLayoutEffect(() => {
    if (prev.current !== pathname) {
      prev.current = pathname;
      abortInFlightGets();
    }
  }, [pathname]);

  return null;
}
