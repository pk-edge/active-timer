import type { ReactNode } from "react";
import Sidebar from "@/components/Sidebar";
import NotificationBell from "@/components/NotificationBell";
import { UserProvider } from "@/hooks/useUser";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <UserProvider>
      <div className="flex h-screen overflow-hidden bg-gray-50">
        <Sidebar />
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Topbar */}
          <header className="h-13 shrink-0 bg-white border-b border-gray-200 flex items-center justify-end px-5 gap-2 shadow-sm">
            <NotificationBell />
          </header>
          {/* Page content */}
          <main className="flex-1 overflow-hidden p-6 flex flex-col">{children}</main>
        </div>
      </div>
    </UserProvider>
  );
}
