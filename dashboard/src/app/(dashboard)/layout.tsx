import type { ReactNode } from "react";
import SWRProvider from "@/components/SWRProvider";
import DashboardShell from "@/components/DashboardShell";
import { UserProvider } from "@/hooks/useUser";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <SWRProvider>
      <UserProvider>
        <DashboardShell>{children}</DashboardShell>
      </UserProvider>
    </SWRProvider>
  );
}
