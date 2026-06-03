import type { ReactNode } from "react";
import Sidebar from "@/components/Sidebar";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen overflow-hidden bg-gray-50">
      <Sidebar />
      <main className="flex-1 overflow-hidden p-6 flex flex-col">{children}</main>
    </div>
  );
}
