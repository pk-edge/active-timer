"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Bell, UserCheck, CheckCheck, X, Inbox } from "lucide-react";
import { getNotifications, markNotificationsRead } from "@/lib/api";
import type { Notification } from "@/types";

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return "";
  const diff = Date.now() - new Date(dateStr).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60)  return "just now";
  const m = Math.floor(s / 60);
  if (m < 60)  return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24)  return `${h}h ago`;
  const d = Math.floor(h / 24);
  return d === 1 ? "yesterday" : `${d}d ago`;
}

const TYPE_CONFIG: Record<string, { color: string; dot: string; href: string }> = {
  approval_request: {
    color: "from-blue-500 to-indigo-600",
    dot:   "bg-blue-500",
    href:  "/users?tab=pending",
  },
};

function NotifItem({ n, onRead }: { n: Notification; onRead: () => void }) {
  const cfg = TYPE_CONFIG[n.type] ?? { color: "from-gray-400 to-gray-500", dot: "bg-gray-400", href: "/" };

  return (
    <Link
      href={cfg.href}
      onClick={onRead}
      className={`flex items-start gap-3.5 px-4 py-3.5 hover:bg-gray-50/80 transition-colors border-b border-gray-100/80 last:border-0 group ${!n.is_read ? "bg-blue-50/40" : ""}`}
    >
      {/* Avatar */}
      <div className={`relative shrink-0 w-11 h-11 rounded-full bg-gradient-to-br ${cfg.color} flex items-center justify-center shadow-sm`}>
        <UserCheck size={20} className="text-white" />
        {!n.is_read && (
          <span className={`absolute -top-0.5 -right-0.5 w-3 h-3 ${cfg.dot} rounded-full border-2 border-white`} />
        )}
      </div>

      {/* Text */}
      <div className="flex-1 min-w-0 pt-0.5">
        <p className={`text-sm leading-snug ${!n.is_read ? "font-semibold text-gray-900" : "font-medium text-gray-700"}`}>
          {n.title}
        </p>
        <p className="text-xs text-gray-500 mt-0.5 truncate">{n.body}</p>
        <p className={`text-[11px] mt-1.5 font-medium ${!n.is_read ? "text-blue-500" : "text-gray-400"}`}>
          {timeAgo(n.created_at)}
        </p>
      </div>

      {/* Approve badge */}
      {n.type === "approval_request" && (
        <span className="shrink-0 text-[10px] font-semibold px-2 py-1 bg-blue-100 text-blue-700 rounded-full self-center opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap">
          Review →
        </span>
      )}
    </Link>
  );
}

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const { data: notifications = [], mutate } = useSWR<Notification[]>(
    "notifications",
    getNotifications,
    { refreshInterval: 30_000 },
  );

  const unread = notifications.filter(n => !n.is_read).length;

  // Close on outside click
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  // Mark all read
  const markAllRead = useCallback(async () => {
    const keys = notifications.filter(n => !n.is_read).map(n => n.key);
    if (!keys.length) return;
    await markNotificationsRead(keys);
    mutate();
  }, [notifications, mutate]);

  // Mark one read
  const markOneRead = useCallback(async (key: string) => {
    if (notifications.find(n => n.key === key)?.is_read) return;
    await markNotificationsRead([key]);
    mutate();
  }, [notifications, mutate]);

  // Bell shake animation on new unread
  const [shake, setShake] = useState(false);
  const prevUnread = useRef(unread);
  useEffect(() => {
    if (unread > prevUnread.current) {
      setShake(true);
      setTimeout(() => setShake(false), 700);
    }
    prevUnread.current = unread;
  }, [unread]);

  return (
    <div ref={ref} className="relative">
      {/* Bell button */}
      <button
        onClick={() => setOpen(o => !o)}
        className={`relative p-2.5 rounded-xl text-gray-500 hover:text-gray-900 hover:bg-gray-100 transition-colors ${shake ? "animate-[wiggle_0.6s_ease-in-out]" : ""}`}
        aria-label="Notifications"
      >
        <Bell size={20} className={open ? "text-blue-600" : ""} />
        {unread > 0 && (
          <span className="absolute top-1.5 right-1.5 min-w-[18px] h-[18px] bg-red-500 text-white text-[10px] font-bold rounded-full flex items-center justify-center px-1 shadow-sm animate-pulse">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {/* Dropdown panel */}
      <div
        className={`absolute right-0 top-full mt-3 w-[calc(100vw-1.5rem)] max-w-[380px] bg-white rounded-2xl shadow-2xl border border-gray-200/80 z-50 overflow-hidden transition-all duration-200 origin-top-right ${
          open ? "opacity-100 scale-100 translate-y-0" : "opacity-0 scale-95 -translate-y-2 pointer-events-none"
        }`}
      >
        {/* Arrow tip */}
        <div className="absolute -top-2 right-4 w-4 h-4 bg-white border-l border-t border-gray-200/80 rotate-45 z-10" />

        {/* Header */}
        <div className="relative flex items-center justify-between px-5 py-3.5 border-b border-gray-100 bg-white z-20">
          <div className="flex items-center gap-2.5">
            <h3 className="font-bold text-gray-900 text-base">Notifications</h3>
            {unread > 0 && (
              <span className="text-xs font-semibold bg-blue-100 text-blue-600 px-2 py-0.5 rounded-full">
                {unread} new
              </span>
            )}
          </div>
          <div className="flex items-center gap-1">
            {unread > 0 && (
              <button
                onClick={markAllRead}
                className="flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-700 font-semibold px-2.5 py-1.5 hover:bg-blue-50 rounded-lg transition"
              >
                <CheckCheck size={13} />
                Mark all read
              </button>
            )}
            <button
              onClick={() => setOpen(false)}
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition"
            >
              <X size={14} />
            </button>
          </div>
        </div>

        {/* Notification list */}
        <div className="max-h-[420px] overflow-y-auto overscroll-contain">
          {notifications.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-14 px-8 text-center">
              <div className="w-16 h-16 bg-gradient-to-br from-gray-100 to-gray-200 rounded-2xl flex items-center justify-center mb-4 shadow-inner">
                <Inbox size={28} className="text-gray-400" />
              </div>
              <p className="text-sm font-semibold text-gray-700">All caught up!</p>
              <p className="text-xs text-gray-400 mt-1.5 leading-relaxed">
                No pending approvals or alerts right now.
              </p>
            </div>
          ) : (
            <>
              {notifications.some(n => !n.is_read) && (
                <p className="px-5 pt-3 pb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">
                  New
                </p>
              )}
              {notifications.filter(n => !n.is_read).map(n => (
                <NotifItem key={n.key} n={n} onRead={() => { markOneRead(n.key); setOpen(false); }} />
              ))}

              {notifications.some(n => n.is_read) && (
                <p className="px-5 pt-3 pb-1 text-[10px] font-bold uppercase tracking-widest text-gray-400">
                  Earlier
                </p>
              )}
              {notifications.filter(n => n.is_read).map(n => (
                <NotifItem key={n.key} n={n} onRead={() => setOpen(false)} />
              ))}
            </>
          )}
        </div>

        {/* Footer */}
        {notifications.length > 0 && (
          <div className="border-t border-gray-100 px-5 py-2.5 bg-gray-50/80">
            <Link
              href="/users?tab=pending"
              onClick={() => setOpen(false)}
              className="block text-center text-xs font-semibold text-blue-600 hover:text-blue-700 py-1 transition"
            >
              View all pending approvals →
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
