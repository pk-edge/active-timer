import type { ReactNode } from "react";
import Sidebar from "@/components/Sidebar";
import { UserProvider } from "@/hooks/useUser";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <UserProvider>
      <div className="flex h-screen overflow-hidden bg-gray-50">
        <Sidebar />
        <main className="flex-1 overflow-hidden p-6 flex flex-col">{children}</main>
      </div>
    </UserProvider>
  );
}
