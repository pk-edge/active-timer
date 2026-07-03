"use client";
import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard, Building2, Users2,
  Monitor, BarChart2, Settings, LogOut, ScrollText,
  UserCog, FolderKanban, Shield, DownloadCloud, ClipboardList, Home,
  Cpu, Tag, Globe, ShieldAlert, X, Paperclip,
  CheckCircle2, AlertCircle, FileDown,
} from "lucide-react";
import clsx from "clsx";
import { adminLogout } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import ConfirmDialog from "@/components/ConfirmDialog";
import { useReportJobs } from "@/contexts/ReportJobsContext";

type NavItem = { href: string; label: string; icon: React.ElementType; module: string };
type NavGroup = { title: string | null; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    title: "Home",
    items: [
      { href: "/home",       label: "Home",                  icon: Home,            module: "home" },
      { href: "/overview",   label: "Organization Overview", icon: Building2,       module: "overview" },
      { href: "/team",       label: "Team Management",       icon: Users2,          module: "team" },
      { href: "/activity",   label: "Activity Dashboard",    icon: LayoutDashboard, module: "activity" },
      { href: "/categories", label: "App Categories",        icon: Tag,             module: "categories" },
    ],
  },
  {
    title: "Monitoring",
    items: [
      { href: "/devices", label: "Devices",       icon: Monitor,    module: "devices" },
      { href: "/logs",    label: "Activity Logs", icon: ScrollText, module: "logs" },
      { href: "/reports", label: "Reports",       icon: BarChart2,  module: "reports" },
    ],
  },
  {
    title: "Technology & AI",
    items: [
      { href: "/tech",             label: "Tech Utilization",  icon: Cpu,   module: "tech" },
      { href: "/tech/websites",    label: "Website Usage",     icon: Globe, module: "tech" },
      { href: "/tech/apps",        label: "Application Usage", icon: Monitor, module: "tech" },
      { href: "/tech/category-usage", label: "Category Usage", icon: Tag,   module: "tech" },
    ],
  },
  {
    title: "Administration",
    items: [
      { href: "/users",    label: "Users",     icon: UserCog,       module: "users" },
      { href: "/groups",   label: "Groups",    icon: FolderKanban,  module: "groups" },
      { href: "/roles",    label: "Roles",     icon: Shield,        module: "roles" },
      { href: "/agents",   label: "Agents",    icon: DownloadCloud, module: "agents" },
      { href: "/policies", label: "URL Policies", icon: ShieldAlert,  module: "policy" },
      { href: "/files",    label: "Shared Files", icon: Paperclip,    module: "files" },
      { href: "/audit",    label: "Audit Log",   icon: ClipboardList, module: "audit" },
      { href: "/settings", label: "Settings",    icon: Settings,      module: "settings" },
    ],
  },
];

// All known hrefs — used to prevent a parent prefix from stealing the active state
// from a more-specific child (e.g. /tech must not stay active when /tech/websites is open)
const ALL_HREFS = NAV_GROUPS.flatMap(g => g.items.map(i => i.href));

function isActive(href: string, path: string): boolean {
  if (path === href) return true;
  if (!path.startsWith(href + "/")) return false;
  // Another nav item is a more-specific match → this one should NOT be active
  return !ALL_HREFS.some(
    h => h !== href && h.startsWith(href + "/") && (path === h || path.startsWith(h + "/")),
  );
}

interface SidebarProps {
  /** Drawer open state — only relevant on mobile (<lg). */
  open?: boolean;
  /** Called to close the drawer (backdrop click / nav click / close button). */
  onClose?: () => void;
}

export default function Sidebar({ open = false, onClose }: SidebarProps) {
  const path = usePathname();
  const { me, can } = useUser();
  const [confirmSignOut, setConfirmSignOut] = useState(false);
  const { jobs, dismiss } = useReportJobs();

  return (
    <>
      {/* Mobile backdrop */}
      {open && (
        <div
          className="fixed inset-0 bg-black/40 z-40 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={clsx(
          "bg-brand-700 flex flex-col shadow-sm w-60 shrink-0",
          // Mobile: fixed slide-out drawer. Desktop: static column.
          "fixed inset-y-0 left-0 z-50 transform transition-transform duration-200 ease-in-out",
          "lg:static lg:translate-x-0 lg:z-auto lg:transition-none",
          open ? "translate-x-0" : "-translate-x-full lg:translate-x-0",
        )}
      >
        <div className="h-14 shrink-0 flex items-center px-5 border-b border-white/10">
          {/* Full wordmark from public/Edge_beacon.svg — white via CSS filter */}
          <img
            src="/Edge_beacon.svg"
            alt="Edge Beacon"
            style={{ height: 30, width: "auto", filter: "brightness(0) invert(1)" }}
          />
          {/* Close button — mobile only */}
          <button
            onClick={onClose}
            aria-label="Close menu"
            className="lg:hidden ml-auto p-1.5 -mr-1.5 text-white/70 hover:text-white rounded-lg hover:bg-white/10"
          >
            <X size={18} />
          </button>
        </div>

        <nav className="flex-1 px-3 py-3 space-y-4 overflow-y-auto sidebar-scroll">
          {NAV_GROUPS.map(group => {
            const visible = group.items.filter(it => can(it.module, "view"));
            if (visible.length === 0) return null;
            return (
              <div key={group.title ?? "x"}>
                {group.title && (
                  <p className="px-3 mb-1 text-[10px] font-semibold uppercase tracking-widest text-white/60">
                    {group.title}
                  </p>
                )}
                <div className="space-y-0.5">
                  {visible.map(({ href, label, icon: Icon }) => {
                    const active = isActive(href, path);
                    return (
                      <Link
                        key={href}
                        href={href}
                        onClick={onClose}
                        className={clsx(
                          "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-all duration-150",
                          active
                            ? "bg-white/20 text-white font-semibold shadow-sm"
                            : "text-white/85 hover:text-white hover:bg-white/12",
                        )}
                      >
                        <Icon size={15} className={active ? "opacity-100" : "opacity-75"} />
                        {label}
                      </Link>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </nav>

        {/* ── Active report downloads ── */}
        {jobs.length > 0 && (
          <div className="px-3 pt-3 pb-2 border-t border-white/10">
            <p className="px-1 mb-2 text-[10px] font-semibold uppercase tracking-widest text-white/50 flex items-center gap-1.5">
              <FileDown size={10} /> Downloads
            </p>
            <div className="space-y-2">
              {jobs.map(job => {
                const isActive = job.status === "pending" || job.status === "running";
                const isDone   = job.status === "done";
                const isFailed = job.status === "failed";
                const fmtLabel = job.format === "both" ? "PDF + XLS" : job.format.toUpperCase();
                return (
                  <div key={job.job_id} className="group bg-white/8 border border-white/10 rounded-xl px-2.5 py-2 hover:bg-white/12 transition-colors">
                    <div className="flex items-start justify-between gap-2 mb-1.5">
                      <div className="min-w-0 flex-1">
                        <p className="text-[11px] font-medium text-white/90 truncate leading-tight">{job.label}</p>
                        <span className="inline-block mt-0.5 text-[9px] bg-white/15 rounded px-1.5 py-px text-white/55 font-medium tracking-wide">
                          {fmtLabel}
                        </span>
                      </div>
                      {isDone   && <CheckCircle2 size={13} className="text-emerald-400 shrink-0 mt-0.5" />}
                      {isFailed && <AlertCircle  size={13} className="text-red-400     shrink-0 mt-0.5" />}
                      {isActive && (
                        <span className="text-[10px] font-semibold text-white/55 tabular-nums shrink-0">
                          {job.progress}%
                        </span>
                      )}
                    </div>

                    {isActive && (
                      <div className="w-full bg-white/15 rounded-full h-1.5 overflow-hidden">
                        <div
                          className="h-full rounded-full transition-all duration-700 ease-out"
                          style={{
                            width: `${Math.max(5, job.progress)}%`,
                            background: "linear-gradient(90deg, #60a5fa 0%, #34d399 100%)",
                          }}
                        />
                      </div>
                    )}

                    {isDone && (
                      <>
                        <div className="w-full rounded-full h-1.5 overflow-hidden" style={{ background: "rgba(52,211,153,0.2)" }}>
                          <div className="h-full w-full rounded-full" style={{ background: "linear-gradient(90deg, #60a5fa 0%, #34d399 100%)" }} />
                        </div>
                        <div className="flex items-center justify-between mt-1.5">
                          <span className="text-[10px] text-emerald-400">Downloaded ✓</span>
                          <button onClick={() => dismiss(job.job_id)} className="text-white/30 hover:text-white/70 transition">
                            <X size={10} />
                          </button>
                        </div>
                      </>
                    )}

                    {isFailed && (
                      <div className="flex items-center justify-between mt-1">
                        <span className="text-[10px] text-red-400 truncate">{job.error ?? "Generation failed"}</span>
                        <button onClick={() => dismiss(job.job_id)} className="text-white/30 hover:text-white/70 transition ml-2 shrink-0">
                          <X size={10} />
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <div className="px-3 py-3 border-t border-white/10">
          {me && (
            <div className="px-3 pb-2">
              <p className="text-xs font-medium text-white truncate">{me.name}</p>
              <p className="text-[11px] text-white/75 truncate">{me.role_name ?? "—"}</p>
            </div>
          )}
          <button
            onClick={() => setConfirmSignOut(true)}
            className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-white/85 hover:text-white hover:bg-white/12 transition-all duration-150 w-full"
          >
            <LogOut size={16} />
            Sign out
          </button>
        </div>
      </aside>

      <ConfirmDialog
        open={confirmSignOut}
        variant="warning"
        title="Sign out?"
        message="You'll be returned to the login screen and will need to sign in again to continue."
        confirmLabel="Sign out"
        onConfirm={adminLogout}
        onCancel={() => setConfirmSignOut(false)}
      />
    </>
  );
}
