import type { ReactNode } from "react";
import DashboardShell from "@/components/DashboardShell";
import { UserProvider } from "@/hooks/useUser";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <UserProvider>
      <DashboardShell>{children}</DashboardShell>
    </UserProvider>
  );
}
