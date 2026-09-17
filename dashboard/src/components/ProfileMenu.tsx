"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { LogOut, UserCog, Search, X, ChevronRight, Loader2 } from "lucide-react";
import {
  adminLogout, getImpersonatableUsers, startImpersonation,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import ConfirmDialog from "@/components/ConfirmDialog";
import type { ImpersonationCandidate } from "@/types";

/**
 * Top-right profile menu.
 *
 * The "View as employee" section renders ONLY when the API says
 * `me.can_impersonate` — which the backend sets exclusively for a real Super
 * Admin acting as themselves. There is no permission to grant, so for every
 * other user this section (and the endpoint behind it) simply does not exist.
 */
export default function ProfileMenu() {
  const { me } = useUser();
  const [open, setOpen] = useState(false);
  const [picking, setPicking] = useState(false);
  const [search, setSearch] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmSignOut, setConfirmSignOut] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const canImpersonate = Boolean(me?.can_impersonate);
  const debounced = useDebouncedValue(search, 250);

  // Only ever fetched once the Super Admin actually opens the picker — a normal
  // page load never touches the endpoint.
  const { data: candidates, isLoading } = useSWR<ImpersonationCandidate[]>(
    canImpersonate && picking ? ["impersonatable", debounced] : null,
    () => getImpersonatableUsers(debounced || undefined),
    { revalidateOnFocus: false },
  );

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setPicking(false);
        setError(null);
      }
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") { setOpen(false); setPicking(false); }
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, []);

  const initials = useMemo(() => {
    if (!me?.name) return "?";
    return me.name.trim().split(/\s+/).slice(0, 2).map(w => w[0]?.toUpperCase()).join("");
  }, [me?.name]);

  async function handlePick(target: ImpersonationCandidate) {
    setBusyId(target.id);
    setError(null);
    try {
      await startImpersonation(target.id);
      // Full reload: every SWR cache entry currently holds the admin's data and
      // the whole app must re-fetch as the target. Cheaper and far safer than
      // trying to selectively invalidate.
      window.location.href = "/home";
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusyId(null);
    }
  }

  if (!me) return null;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => { setOpen(o => !o); setPicking(false); setError(null); }}
        aria-label="Account menu"
        aria-expanded={open}
        className="flex items-center justify-center w-9 h-9 rounded-xl bg-brand-600 text-white text-xs font-bold hover:bg-brand-700 transition-colors"
      >
        {initials}
      </button>

      <div
        className={`absolute right-0 top-full mt-3 w-[calc(100vw-1.5rem)] max-w-[340px] bg-white rounded-2xl shadow-2xl border border-gray-200/80 z-50 overflow-hidden transition-all duration-200 origin-top-right ${
          open ? "opacity-100 scale-100 translate-y-0" : "opacity-0 scale-95 -translate-y-2 pointer-events-none"
        }`}
      >
        <div className="absolute -top-2 right-4 w-4 h-4 bg-white border-l border-t border-gray-200/80 rotate-45 z-10" />

        {/* Identity */}
        <div className="relative z-20 px-4 py-3.5 border-b border-gray-100 bg-white">
          <p className="text-sm font-semibold text-gray-900 truncate">{me.name}</p>
          <p className="text-xs text-gray-600 truncate">{me.email}</p>
          {me.role_name && (
            <span className="inline-block mt-1.5 text-[10px] font-semibold bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">
              {me.role_name}
            </span>
          )}
        </div>

        {canImpersonate && !picking && (
          <button
            onClick={() => setPicking(true)}
            className="w-full flex items-center justify-between gap-2 px-4 py-3 text-sm text-gray-700 hover:bg-gray-50 transition"
          >
            <span className="flex items-center gap-2.5">
              <UserCog size={16} className="text-brand-600" />
              View as employee
            </span>
            <ChevronRight size={14} className="text-gray-400" />
          </button>
        )}

        {canImpersonate && picking && (
          <div className="border-b border-gray-100">
            <div className="px-3 pt-3 pb-2">
              <div className="relative">
                <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                <input
                  autoFocus
                  type="text"
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                  placeholder="Search employees…"
                  className="w-full pl-8 pr-8 py-1.5 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                />
                {search && (
                  <button
                    onClick={() => setSearch("")}
                    aria-label="Clear search"
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                  >
                    <X size={13} />
                  </button>
                )}
              </div>
            </div>

            {error && (
              <p className="px-4 pb-2 text-xs text-red-600">{error}</p>
            )}

            <div className="max-h-64 overflow-y-auto pb-2">
              {isLoading && (
                <p className="px-4 py-6 text-center text-xs text-gray-500 flex items-center justify-center gap-2">
                  <Loader2 size={13} className="animate-spin" /> Loading…
                </p>
              )}

              {!isLoading && candidates?.length === 0 && (
                <div className="px-4 py-6 text-center">
                  <p className="text-xs text-gray-600">
                    {search ? `No employees match "${search}".` : "No employees available to view as."}
                  </p>
                  {search && (
                    <button
                      onClick={() => setSearch("")}
                      className="mt-2 text-xs font-medium text-brand-600 hover:text-brand-700"
                    >
                      Clear search
                    </button>
                  )}
                </div>
              )}

              {candidates?.map(c => (
                <button
                  key={c.id}
                  disabled={busyId !== null}
                  onClick={() => handlePick(c)}
                  className="w-full text-left px-4 py-2 hover:bg-gray-50 transition disabled:opacity-50 flex items-center justify-between gap-2"
                >
                  <span className="min-w-0">
                    <span className="block text-sm text-gray-800 truncate">{c.name}</span>
                    <span className="block text-[11px] text-gray-500 truncate">{c.email}</span>
                  </span>
                  <span className="shrink-0 flex items-center gap-1.5">
                    {c.role_name && (
                      <span className="text-[10px] text-gray-500 bg-gray-100 rounded px-1.5 py-0.5">
                        {c.role_name}
                      </span>
                    )}
                    {busyId === c.id && <Loader2 size={12} className="animate-spin text-brand-600" />}
                  </span>
                </button>
              ))}
            </div>

            <button
              onClick={() => { setPicking(false); setError(null); }}
              className="w-full px-4 py-2 text-xs text-gray-500 hover:text-gray-700 hover:bg-gray-50 transition border-t border-gray-100"
            >
              Cancel
            </button>
          </div>
        )}

        <button
          onClick={() => { setOpen(false); setConfirmSignOut(true); }}
          className="w-full flex items-center gap-2.5 px-4 py-3 text-sm text-gray-700 hover:bg-gray-50 transition"
        >
          <LogOut size={16} className="text-gray-500" />
          Sign out
        </button>
      </div>

      <ConfirmDialog
        open={confirmSignOut}
        title="Sign out?"
        message="You'll need to sign in again to access the dashboard."
        confirmLabel="Sign out"
        onConfirm={adminLogout}
        onCancel={() => setConfirmSignOut(false)}
      />
    </div>
  );
}
