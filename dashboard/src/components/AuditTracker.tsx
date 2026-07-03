"use client";
import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { flush, flushOnHide, logEvent } from "@/lib/auditLogger";

// Mounted once in the dashboard layout — tracks page views and manages the
// flush lifecycle (periodic timer + tab-hide keepalive flush).
export default function AuditTracker() {
  const pathname = usePathname();
  const prevPath = useRef<string | null>(null);

  useEffect(() => {
    const meta: Record<string, unknown> = { path: pathname };
    if (prevPath.current !== null) meta.from = prevPath.current;
    logEvent("page.view", "page", undefined, meta);
    prevPath.current = pathname;
  }, [pathname]);

  useEffect(() => {
    const interval = setInterval(() => { void flush(); }, 3000);

    const onHide = () => {
      if (document.visibilityState === "hidden") flushOnHide();
    };
    document.addEventListener("visibilitychange", onHide);

    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onHide);
      void flush();
    };
  }, []);

  return null;
}
