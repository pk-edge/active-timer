"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard, Building2, Users2,
  Monitor, BarChart2, Settings, Timer, LogOut, ScrollText,
  UserCog, FolderKanban, Shield, DownloadCloud, ClipboardList, Home,
  Cpu, Tag, Globe, ShieldAlert, X,
} from "lucide-react";
import clsx from "clsx";
import { adminLogout } from "@/lib/api";
import { useUser } from "@/hooks/useUser";

type NavItem = { href: string; label: string; icon: React.ElementType; module: string };
type NavGroup = { title: string | null; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    title: "Home",
    items: [
      { href: "/home",       label: "Home",                  icon: Home,            module: "home" },
      { href: "/overview",   label: "Organization Overview", icon: Building2,       module: "home" },
      { href: "/team",       label: "Team Management",       icon: Users2,          module: "home" },
      { href: "/activity",   label: "Activity Dashboard",    icon: LayoutDashboard, module: "home" },
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
      { href: "/policies", label: "URL Policies", icon: ShieldAlert, module: "policy" },
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
          "bg-white border-r border-gray-200 flex flex-col shadow-sm w-60 shrink-0",
          // Mobile: fixed slide-out drawer. Desktop: static column.
          "fixed inset-y-0 left-0 z-50 transform transition-transform duration-200 ease-in-out",
          "lg:static lg:translate-x-0 lg:z-auto lg:transition-none",
          open ? "translate-x-0" : "-translate-x-full lg:translate-x-0",
        )}
      >
        <div className="flex items-center gap-2.5 px-5 py-5 border-b border-gray-200">
          <div className="w-7 h-7 bg-blue-600 rounded-lg flex items-center justify-center">
            <Timer size={14} className="text-white" />
          </div>
          <span className="font-semibold text-sm text-gray-900">TimeTracker</span>
          {/* Close button — mobile only */}
          <button
            onClick={onClose}
            aria-label="Close menu"
            className="lg:hidden ml-auto p-1.5 -mr-1.5 text-gray-400 hover:text-gray-700 rounded-lg hover:bg-gray-100"
          >
            <X size={18} />
          </button>
        </div>

        <nav className="flex-1 px-3 py-3 space-y-4 overflow-y-auto">
          {NAV_GROUPS.map(group => {
            const visible = group.items.filter(it => can(it.module, "view"));
            if (visible.length === 0) return null;
            return (
              <div key={group.title ?? "x"}>
                {group.title && (
                  <p className="px-3 mb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
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
                          "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition",
                          active
                            ? "bg-blue-50 text-blue-600 font-medium"
                            : "text-gray-600 hover:text-gray-900 hover:bg-gray-50",
                        )}
                      >
                        <Icon size={16} />
                        {label}
                      </Link>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </nav>

        <div className="px-3 py-3 border-t border-gray-200">
          {me && (
            <div className="px-3 pb-2">
              <p className="text-xs font-medium text-gray-700 truncate">{me.name}</p>
              <p className="text-[11px] text-gray-400 truncate">{me.role_name ?? "—"}</p>
            </div>
          )}
          <button
            onClick={adminLogout}
            className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-gray-500 hover:text-gray-900 hover:bg-gray-50 transition w-full"
          >
            <LogOut size={16} />
            Sign out
          </button>
        </div>
      </aside>
    </>
  );
}
