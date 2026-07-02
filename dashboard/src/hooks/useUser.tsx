"use client";
import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { useRouter } from "next/navigation";
import { getMe, isAuthenticated } from "@/lib/api";
import PageLoader from "@/components/PageLoader";
import type { Me } from "@/types";

interface UserCtx {
  me:      Me | null;
  loading: boolean;
  can:     (module: string, action?: string) => boolean;
}

const Ctx = createContext<UserCtx>({ me: null, loading: true, can: () => false });

type AuthStatus = "checking" | "authed" | "anon";

export function UserProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [status, setStatus] = useState<AuthStatus>("checking");

  useEffect(() => {
    if (!isAuthenticated()) {
      // No token at all → not logged in. Redirect to /login WITHOUT ever
      // rendering the dashboard shell, so there is no home/dashboard flash.
      setStatus("anon");
      router.replace("/login");
      return;
    }
    getMe()
      .then((m) => { setMe(m); setStatus("authed"); })
      .catch(() => { setStatus("anon"); router.replace("/login"); });
  }, [router]);

  function can(module: string, action: string = "view"): boolean {
    if (!me) return false;
    // Always check the permissions array — the backend populates it correctly for
    // every role including Super Admin. Relying on scope_type === "all" was wrong:
    // scope_type controls data visibility (which devices), not feature access, so a
    // non-system role with scope_type "all" would incorrectly bypass all checks.
    return me.permissions.some(p => p.module === module && p.action === action);
  }

  // Until we KNOW the visitor is authenticated, render a neutral full-screen
  // splash — never the dashboard chrome. "checking" covers the brief session
  // verification for a real user; "anon" covers the redirect to /login. The
  // dashboard (children) only mounts once status === "authed".
  if (status !== "authed" || !me) {
    return <PageLoader />;
  }

  return <Ctx.Provider value={{ me, loading: false, can }}>{children}</Ctx.Provider>;
}

export function useUser() {
  return useContext(Ctx);
}
