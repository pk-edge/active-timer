"use client";
import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { useRouter } from "next/navigation";
import { getMe, isAuthenticated } from "@/lib/api";
import type { Me } from "@/types";

interface UserCtx {
  me:      Me | null;
  loading: boolean;
  can:     (module: string, action?: string) => boolean;
}

const Ctx = createContext<UserCtx>({ me: null, loading: true, can: () => false });

export function UserProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isAuthenticated()) {
      router.replace("/login");
      return;
    }
    getMe()
      .then(setMe)
      .catch(() => router.replace("/login"))
      .finally(() => setLoading(false));
  }, [router]);

  function can(module: string, action: string = "view"): boolean {
    if (!me) return false;
    if (me.scope_type === "all") return true;   // Super Admin shortcut
    return me.permissions.some(p => p.module === module && p.action === action);
  }

  return <Ctx.Provider value={{ me, loading, can }}>{children}</Ctx.Provider>;
}

export function useUser() {
  return useContext(Ctx);
}
