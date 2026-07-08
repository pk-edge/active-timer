"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@/hooks/useUser";
import PageLoader from "@/components/PageLoader";

// Land on the first page the current user is permitted to see.
export default function RootPage() {
  const router = useRouter();
  const { loading } = useUser();

  useEffect(() => {
    if (loading) return;
    // Always land on /home first (it adapts per role)
    router.replace("/home");
  }, [loading, router]);

  // Show the same splash as everywhere else so the auth-check → /home hop is a
  // single, seamless loader rather than a flash of plain "Loading…" text.
  return <PageLoader />;
}
