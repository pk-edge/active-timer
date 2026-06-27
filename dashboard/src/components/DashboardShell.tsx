"use client";
import { useState, useCallback, type ReactNode } from "react";
import { Menu, RefreshCw } from "lucide-react";
import { useSWRConfig } from "swr";
import { format } from "date-fns";
import Sidebar from "@/components/Sidebar";
import NotificationBell from "@/components/NotificationBell";
import EdgeLogo from "@/components/EdgeLogo";
import { useUser } from "@/hooks/useUser";

/**
 * Client shell that owns the mobile drawer state.
 *
 * - Desktop (lg+): Sidebar is a static column; header shows greeting + refresh + bell.
 * - Mobile (<lg): Sidebar is a slide-out drawer; header shows hamburger + logo + refresh + bell.
 */
export default function DashboardShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [spinning, setSpinning] = useState(false);
  const { me } = useUser();
  const { mutate } = useSWRConfig();

  const handleRefresh = useCallback(async () => {
    if (spinning) return;
    setSpinning(true);
    const started = Date.now();
    try {
      await mutate(() => true, undefined, { revalidate: true });
    } finally {
      const elapsed = Date.now() - started;
      if (elapsed < 450) await new Promise(r => setTimeout(r, 450 - elapsed));
      setSpinning(false);
    }
  }, [mutate, spinning]);

  const todayLabel = format(new Date(), "EEEE, MMMM d");

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50">
      <Sidebar open={open} onClose={() => setOpen(false)} />

      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        {/* Topbar — matches sidebar header h-14 exactly */}
        <header className="h-14 shrink-0 bg-white border-b border-gray-200 flex items-center px-3 sm:px-4 lg:px-5 gap-2 shadow-sm">

          {/* Mobile-only: hamburger */}
          <button
            onClick={() => setOpen(true)}
            aria-label="Open menu"
            className="lg:hidden p-2 -ml-1 text-gray-600 hover:text-gray-900 rounded-lg hover:bg-gray-100 transition"
          >
            <Menu size={20} />
          </button>

          {/* Mobile-only: compact logo */}
          <div className="lg:hidden flex items-center gap-2 shrink-0">
            <EdgeLogo variant="icon" size={26} />
            <span className="font-semibold text-sm text-gray-900 hidden xs:block">Time Tracker</span>
          </div>

          {/* Desktop: welcome greeting */}
          {me && (
            <div className="hidden lg:flex flex-col justify-center min-w-0 leading-tight">
              <p className="text-sm font-semibold text-gray-900 truncate">
                Welcome back, {me.name}!
              </p>
              <p className="text-xs text-gray-600 truncate mt-0.5">
                <span className="font-medium text-brand-600">{me.role_name}</span>
                {" · "}
                {todayLabel}
              </p>
            </div>
          )}

          {/* Spacer */}
          <div className="flex-1" />

          {/* Right side: refresh + notifications */}
          <div className="flex items-center gap-1">
            <button
              onClick={handleRefresh}
              disabled={spinning}
              aria-label="Refresh"
              className="p-2.5 rounded-xl text-gray-600 hover:text-gray-900 hover:bg-gray-100 transition-colors disabled:opacity-50"
            >
              <RefreshCw size={18} className={spinning ? "animate-spin" : ""} />
            </button>
            <NotificationBell />
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-hidden p-3 sm:p-4 lg:p-6 flex flex-col">
          {children}
        </main>
      </div>
    </div>
  );
}
