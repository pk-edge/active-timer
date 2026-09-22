"use client";
import { useState } from "react";
import { ShieldAlert, LogOut, Loader2 } from "lucide-react";
import { exitImpersonation } from "@/lib/api";
import { useUser } from "@/hooks/useUser";

/**
 * Sticky warning bar shown for the whole duration of an impersonation session.
 *
 * Rendered as a flex sibling ABOVE the dashboard shell rather than as an
 * overlay: the shell is `h-screen overflow-hidden`, so an absolutely positioned
 * bar would cover content instead of displacing it.
 */
export default function ImpersonationBanner() {
  const { me } = useUser();
  const [exiting, setExiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!me?.impersonating) return null;

  async function handleExit() {
    setExiting(true);
    setError(null);
    try {
      await exitImpersonation();
      // Full reload: every cached SWR entry belongs to the impersonated user.
      window.location.href = "/home";
    } catch (e) {
      // The session is already revoked server-side even when the handback is
      // refused (e.g. the admin account was deactivated mid-session), so the
      // only honest next step is a fresh sign-in.
      setError(e instanceof Error ? e.message : String(e));
      setExiting(false);
    }
  }

  return (
    <div className="shrink-0 bg-amber-500 text-amber-950 border-b border-amber-600/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 sm:px-4 lg:px-5 py-2">
        <ShieldAlert size={16} className="shrink-0" />

        <p className="text-sm font-medium min-w-0">
          You are viewing the system as{" "}
          <span className="font-bold">{me.name}</span>{" "}
          <span className="font-normal opacity-80">({me.email})</span>
        </p>

        <div className="flex-1" />

        {error && (
          <span className="text-xs font-medium bg-amber-950/10 rounded px-2 py-1">
            {error}
          </span>
        )}

        <button
          onClick={handleExit}
          disabled={exiting}
          className="shrink-0 inline-flex items-center gap-1.5 rounded-lg bg-amber-950 px-3 py-1.5 text-xs font-semibold text-amber-50 hover:bg-amber-900 transition disabled:opacity-60"
        >
          {exiting ? <Loader2 size={13} className="animate-spin" /> : <LogOut size={13} />}
          {exiting ? "Returning…" : "Exit impersonation"}
        </button>
      </div>
    </div>
  );
}
