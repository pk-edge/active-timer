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
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        if (can(item.module, "view")) {
          router.replace(item.href);
          return;
        }
      }
    }
  }, [loading, can, router]);

  return (
    <div className="flex items-center justify-center h-full text-gray-400 text-sm">
      Loading…
    </div>
  );
}
