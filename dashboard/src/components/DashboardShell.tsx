"use client";
import { useState, type ReactNode } from "react";
import { Menu, Timer } from "lucide-react";
import Sidebar from "@/components/Sidebar";
import NotificationBell from "@/components/NotificationBell";

/**
 * Client shell that owns the mobile drawer state.
 *
 * - Desktop (lg+): Sidebar is a static column; header shows only the bell.
 * - Mobile (<lg): Sidebar is a slide-out drawer toggled by the hamburger;
 *   header shows hamburger + logo on the left, bell on the right.
 */
export default function DashboardShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50">
      <Sidebar open={open} onClose={() => setOpen(false)} />

      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        {/* Topbar */}
        <header className="h-14 shrink-0 bg-white border-b border-gray-200 flex items-center px-3 sm:px-5 gap-2 shadow-sm">
          {/* Mobile-only: hamburger + compact logo */}
          <button
            onClick={() => setOpen(true)}
            aria-label="Open menu"
            className="lg:hidden p-2 -ml-1 text-gray-600 hover:text-gray-900 rounded-lg hover:bg-gray-100 transition"
          >
            <Menu size={20} />
          </button>
          <div className="lg:hidden flex items-center gap-2">
            <div className="w-6 h-6 bg-brand-600 rounded-lg flex items-center justify-center">
              <Timer size={12} className="text-white" />
            </div>
            <span className="font-semibold text-sm text-gray-900">TimeTracker</span>
          </div>

          <div className="flex-1" />
          <NotificationBell />
        </header>

        {/* Page content — tighter padding on mobile */}
        <main className="flex-1 overflow-hidden p-3 sm:p-4 lg:p-6 flex flex-col">
          {children}
        </main>
      </div>
    </div>
  );
}
