import type { ReactNode } from "react";
import SWRProvider from "@/components/SWRProvider";
import DashboardShell from "@/components/DashboardShell";
import NavigationAbort from "@/components/NavigationAbort";
import AuditTracker from "@/components/AuditTracker";
import { UserProvider } from "@/hooks/useUser";
import { ReportJobsProvider } from "@/contexts/ReportJobsContext";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <SWRProvider>
      <UserProvider>
        <ReportJobsProvider>
          <NavigationAbort />
          <AuditTracker />
          <DashboardShell>{children}</DashboardShell>
        </ReportJobsProvider>
      </UserProvider>
    </SWRProvider>
  );
}
