"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@/hooks/useUser";
import { NAV_GROUPS } from "@/components/Sidebar";

// Land on the first page the current user is permitted to see.
export default function RootPage() {
  const router = useRouter();
  const { loading, can } = useUser();

  useEffect(() => {
    if (loading) return;
    // Always land on /home first (it adapts per role)
    router.replace("/home");
  }, [loading, router]);

  return (
    <div className="flex items-center justify-center h-full text-gray-400 text-sm">
      Loading…
    </div>
  );
}
