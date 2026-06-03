"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Monitor, BarChart2, Settings, Timer, LogOut, ScrollText } from "lucide-react";
import clsx from "clsx";
import { adminLogout } from "@/lib/api";

const NAV = [
  { href: "/",        label: "Devices",       icon: Monitor    },
  { href: "/logs",    label: "Activity Logs", icon: ScrollText },
  { href: "/reports", label: "Reports",       icon: BarChart2  },
  { href: "/settings",label: "Settings",      icon: Settings   },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="w-56 bg-white border-r border-gray-200 flex flex-col shrink-0 shadow-sm">
      <div className="flex items-center gap-2.5 px-5 py-5 border-b border-gray-200">
        <div className="w-7 h-7 bg-blue-600 rounded-lg flex items-center justify-center">
          <Timer size={14} className="text-white" />
        </div>
        <span className="font-semibold text-sm text-gray-900">TimeTracker</span>
      </div>

      <nav className="flex-1 px-3 py-4 space-y-0.5">
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className={clsx(
              "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition",
              path === href || (href !== "/" && path.startsWith(href))
                ? "bg-blue-50 text-blue-600 font-medium"
                : "text-gray-600 hover:text-gray-900 hover:bg-gray-50",
            )}
          >
            <Icon size={16} />
            {label}
          </Link>
        ))}
      </nav>

      <div className="px-3 py-3 border-t border-gray-200">
        <button
          onClick={adminLogout}
          className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-gray-500 hover:text-gray-900 hover:bg-gray-50 transition w-full"
        >
          <LogOut size={16} />
          Sign out
        </button>
      </div>
    </aside>
  );
}
