"use client";
import { useState, useMemo } from "react";
import useSWR from "swr";
import {
  Shield, Plus, Trash2, Edit2, X, ChevronDown, Search,
  Globe, Users2, Monitor, ToggleLeft, ToggleRight, AlertTriangle,
  CheckSquare, Square, Tag,
} from "lucide-react";
import {
  getBlockedUrls, createBlockedUrl, updateBlockedUrl, deleteBlockedUrl,
  getDetectedUrls, getDevices, getUsers,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import ConfirmDialog from "@/components/ConfirmDialog";
import type { BlockedUrl, Device, User } from "@/types";

// ── Detected-URL multi-select picker ─────────────────────────────────────────

interface DetectedPickerProps {
  selected:    Set<string>;
  onToggle:    (host: string) => void;
  existing:    Set<string>;   // already blocked — shown greyed out
}

function DetectedPicker({ selected, onToggle, existing }: DetectedPickerProps) {
  const [search, setSearch] = useState("");
  const { data, isLoading } = useSWR<{ host: string; visits: number }[]>(
    "detected-urls", getDetectedUrls, { revalidateOnFocus: false }
  );

  const filtered = useMemo(() => {
    const list = data ?? [];
    if (!search.trim()) return list;
    return list.filter(d => d.host.toLowerCase().includes(search.toLowerCase()));
  }, [data, search]);

  return (
    <div className="border border-gray-200 rounded-xl overflow-hidden">
      {/* Search inside picker */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-100 bg-gray-50">
        <Search size={13} className="text-gray-400 shrink-0" />
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Filter detected sites…"
          className="flex-1 bg-transparent text-sm outline-none placeholder-gray-400"
        />
        {search && (
          <button onClick={() => setSearch("")} className="text-gray-400 hover:text-gray-600">
            <X size={12} />
          </button>
        )}
        <span className="text-xs text-gray-400 shrink-0">
          {selected.size > 0 ? `${selected.size} selected` : `${filtered.length} sites`}
        </span>
      </div>

      {/* URL list */}
      <div className="max-h-52 overflow-y-auto divide-y divide-gray-100">
        {isLoading ? (
          <p className="text-xs text-gray-400 py-4 text-center">Loading detected sites…</p>
        ) : filtered.length === 0 ? (
          <p className="text-xs text-gray-400 py-4 text-center">
            {search ? "No matches" : "No sites detected yet (requires URL visit activity)"}
          </p>
        ) : (
          filtered.map(({ host, visits }) => {
            const isSelected = selected.has(host);
            const isExisting = existing.has(host);
            return (
              <button
                key={host}
                type="button"
                disabled={isExisting}
                onClick={() => !isExisting && onToggle(host)}
                className={`w-full flex items-center gap-3 px-3 py-2 text-left transition ${
                  isExisting
                    ? "opacity-40 cursor-not-allowed"
                    : isSelected
                    ? "bg-red-50"
                    : "hover:bg-gray-50"
                }`}
              >
                {isSelected
                  ? <CheckSquare size={14} className="text-red-500 shrink-0" />
                  : <Square     size={14} className="text-gray-300 shrink-0" />}
                <span className="flex-1 text-sm text-gray-800 truncate">{host}</span>
                <span className="text-xs text-gray-400 shrink-0">{visits.toLocaleString()} visits</span>
                {isExisting && <span className="text-[10px] text-gray-400 shrink-0">already blocked</span>}
              </button>
            );
          })
        )}
      </div>

      {/* Select-all shortcut when there are results */}
      {filtered.length > 0 && (
        <div className="px-3 py-1.5 border-t border-gray-100 bg-gray-50 flex items-center gap-3">
          <button
            type="button"
            onClick={() => filtered.filter(d => !existing.has(d.host)).forEach(d => {
              if (!selected.has(d.host)) onToggle(d.host);
            })}
            className="text-xs text-brand-600 hover:underline"
          >
            Select all visible
          </button>
          {selected.size > 0 && (
            <button
              type="button"
              onClick={() => Array.from(selected).forEach(h => onToggle(h))}
              className="text-xs text-gray-500 hover:underline"
            >
              Clear selection
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ── Rule form modal ───────────────────────────────────────────────────────────

interface RuleFormProps {
  initial?:     BlockedUrl;
  devices:      Device[];
  users:        User[];
  existingRules: BlockedUrl[];
  onClose:      () => void;
  onSave:       () => void;
}

function RuleForm({ initial, devices, users, existingRules, onClose, onSave }: RuleFormProps) {
  // When editing we show a single pattern; when creating allow multi-select + manual
  const isEdit = Boolean(initial);

  const [manualPattern, setManualPattern] = useState(initial?.pattern ?? "");
  const [selected,      setSelected]      = useState<Set<string>>(new Set());
  const [targetType,    setTargetType]    = useState<"all" | "device" | "employee">(
    (initial?.target_type as "all" | "device" | "employee") ?? "all"
  );
  const [deviceId,   setDeviceId]   = useState(initial?.device_id   ?? "");
  const [employeeId, setEmployeeId] = useState(initial?.employee_id ?? "");
  const [notes,      setNotes]      = useState(initial?.notes       ?? "");
  const [saving,     setSaving]     = useState(false);
  const [err,        setErr]        = useState("");

  // Existing blocked patterns for the greyed-out state in the picker
  const existingSet = useMemo(
    () => new Set(existingRules.filter(r => r.is_active).map(r => r.pattern)),
    [existingRules]
  );

  function toggleHost(host: string) {
    setSelected(prev => {
      const next = new Set(prev);
      next.has(host) ? next.delete(host) : next.add(host);
      return next;
    });
  }

  // Combined patterns to block: manual input + multi-selected detected URLs
  const allPatterns = useMemo(() => {
    const set = new Set<string>();
    if (manualPattern.trim()) set.add(manualPattern.trim().toLowerCase());
    selected.forEach(h => set.add(h));
    return Array.from(set);
  }, [manualPattern, selected]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (allPatterns.length === 0 && !isEdit) { setErr("Select or type at least one URL"); return; }
    if (isEdit && !manualPattern.trim()) { setErr("URL pattern is required"); return; }
    setSaving(true);
    try {
      const scope = {
        target_type: targetType,
        device_id:   targetType === "device"   ? deviceId   || undefined : undefined,
        employee_id: targetType === "employee" ? employeeId || undefined : undefined,
        notes:       notes || undefined,
      };
      if (isEdit && initial) {
        await updateBlockedUrl(initial.id, { pattern: manualPattern.trim().toLowerCase(), ...scope });
      } else {
        // Create one rule per selected/typed pattern
        await Promise.all(
          allPatterns.map(p => createBlockedUrl({ pattern: p, ...scope }))
        );
      }
      onSave();
      onClose();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[92vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 shrink-0">
          <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
            <Shield size={16} className="text-red-500" />
            {isEdit ? "Edit Block Rule" : "Block URLs"}
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>

        <form onSubmit={submit} className="flex flex-col flex-1 min-h-0 overflow-y-auto">
          <div className="px-6 py-4 space-y-4">
            {/* ── Detected URLs picker (create mode only) ── */}
            {!isEdit && (
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1.5">
                  Sites from Your Devices
                  <span className="ml-1 text-gray-400 font-normal">(select to add to block list)</span>
                </label>
                <DetectedPicker
                  selected={selected}
                  onToggle={toggleHost}
                  existing={existingSet}
                />
              </div>
            )}

            {/* ── Selected pills ── */}
            {!isEdit && selected.size > 0 && (
              <div>
                <p className="text-xs text-gray-500 mb-1.5">
                  Selected ({selected.size}):
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {Array.from(selected).map(h => (
                    <span key={h}
                          className="inline-flex items-center gap-1 text-xs bg-red-50 text-red-700 border border-red-200 px-2 py-0.5 rounded-full">
                      {h}
                      <button type="button" onClick={() => toggleHost(h)} className="hover:text-red-900">
                        <X size={10} />
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* ── Manual URL input ── */}
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1.5">
                {isEdit ? "URL Pattern" : "Or type a custom URL / domain"}
              </label>
              <input
                value={manualPattern}
                onChange={e => { setManualPattern(e.target.value); setErr(""); }}
                placeholder="e.g. facebook.com or news.ycombinator.com"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-200"
              />
              {!isEdit && (
                <p className="text-xs text-gray-400 mt-1">
                  Enter a domain to block all pages, or a subdomain for a specific section.
                </p>
              )}
            </div>

            {/* ── Apply To scope ── */}
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1.5">Apply To</label>
              <div className="relative">
                <select
                  value={targetType}
                  onChange={e => setTargetType(e.target.value as "all" | "device" | "employee")}
                  className="w-full appearance-none border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-200"
                >
                  <option value="all">All users (company-wide)</option>
                  <option value="employee">Specific user</option>
                  <option value="device">Specific device</option>
                </select>
                <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              </div>
            </div>

            {targetType === "employee" && (
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1.5">User</label>
                <div className="relative">
                  <select value={employeeId} onChange={e => setEmployeeId(e.target.value)}
                          className="w-full appearance-none border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-200">
                    <option value="">— select user —</option>
                    {users.filter(u => u.is_active).map(u => (
                      <option key={u.id} value={u.id}>{u.name} ({u.email})</option>
                    ))}
                  </select>
                  <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                </div>
              </div>
            )}

            {targetType === "device" && (
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1.5">Device</label>
                <div className="relative">
                  <select value={deviceId} onChange={e => setDeviceId(e.target.value)}
                          className="w-full appearance-none border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-200">
                    <option value="">— select device —</option>
                    {devices.map(d => (
                      <option key={d.id} value={d.id}>
                        {d.employee_name ? `${d.employee_name} — ` : ""}{d.hostname}
                      </option>
                    ))}
                  </select>
                  <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                </div>
              </div>
            )}

            {/* Notes */}
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1.5">Notes (optional)</label>
              <input
                value={notes}
                onChange={e => setNotes(e.target.value)}
                placeholder="Why is this blocked?"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-200"
              />
            </div>

            {err && <p className="text-xs text-red-500">{err}</p>}
          </div>

          {/* Footer */}
          <div className="px-6 pb-5 pt-2 flex gap-2 shrink-0 border-t border-gray-100 mt-auto">
            <button type="button" onClick={onClose}
                    className="flex-1 border border-gray-200 text-gray-600 rounded-lg py-2 text-sm hover:bg-gray-50 transition">
              Cancel
            </button>
            <button type="submit" disabled={saving || (!isEdit && allPatterns.length === 0)}
                    className="flex-1 bg-red-600 text-white rounded-lg py-2 text-sm font-medium hover:bg-red-700 transition disabled:opacity-50">
              {saving
                ? "Saving…"
                : isEdit
                ? "Save Changes"
                : allPatterns.length > 1
                ? `Block ${allPatterns.length} URLs`
                : "Block URL"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Rule row ──────────────────────────────────────────────────────────────────

function TargetBadge({ rule, devices, users }: { rule: BlockedUrl; devices: Device[]; users: User[] }) {
  if (rule.target_type === "all") {
    return (
      <span className="inline-flex items-center gap-1 text-xs bg-orange-50 text-orange-700 border border-orange-200 px-2 py-0.5 rounded-full">
        <Users2 size={10} /> All users
      </span>
    );
  }
  if (rule.target_type === "device") {
    const dev = devices.find(d => d.id === rule.device_id);
    return (
      <span className="inline-flex items-center gap-1 text-xs bg-brand-50 text-brand-700 border border-brand-200 px-2 py-0.5 rounded-full">
        <Monitor size={10} /> {dev?.hostname ?? "Device"}
      </span>
    );
  }
  const user = users.find(u => u.id === rule.employee_id);
  return (
    <span className="inline-flex items-center gap-1 text-xs bg-purple-50 text-purple-700 border border-purple-200 px-2 py-0.5 rounded-full">
      <Users2 size={10} /> {user?.name ?? "User"}
    </span>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function PoliciesPage() {
  const { loading, can } = useUser();
  const [showForm, setShowForm] = useState(false);
  const [editing,  setEditing]  = useState<BlockedUrl | undefined>(undefined);
  const [search,   setSearch]   = useState("");
  const [filter,   setFilter]   = useState<"all" | "active" | "inactive">("active");

  const { data: rules,   mutate } = useSWR<BlockedUrl[]>("blocked-urls", () => getBlockedUrls());
  const { data: devices }         = useSWR<Device[]>("devices", getDevices);
  const { data: users }           = useSWR<User[]>("users-policy", () => getUsers());

  const canEdit = can("policy", "edit");

  const filtered = useMemo(() => {
    let list = rules ?? [];
    if (filter === "active")   list = list.filter(r => r.is_active);
    if (filter === "inactive") list = list.filter(r => !r.is_active);
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(r => r.pattern.includes(q) || (r.notes ?? "").toLowerCase().includes(q));
    }
    return list;
  }, [rules, filter, search]);

  async function toggleActive(rule: BlockedUrl) {
    await updateBlockedUrl(rule.id, { is_active: !rule.is_active });
    mutate();
  }

  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  async function confirmDelete() {
    if (!pendingDeleteId) return;
    setDeleting(true);
    try { await deleteBlockedUrl(pendingDeleteId); mutate(); setPendingDeleteId(null); }
    finally { setDeleting(false); }
  }

  if (loading) return null;
  if (!can("policy", "view")) return <AccessDenied label="URL Policies" />;

  const totalActive   = (rules ?? []).filter(r => r.is_active).length;
  const totalInactive = (rules ?? []).filter(r => !r.is_active).length;
  const companyWide   = (rules ?? []).filter(r => r.target_type === "all" && r.is_active).length;

  return (
    <div className="overflow-y-auto pr-1">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900 flex items-center gap-2">
            <Shield size={22} className="text-red-500" />
            URL Policies
          </h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Block websites and URLs for specific users, devices, or company-wide.
            Rules sync to agents every 5 minutes and are enforced at OS level.
          </p>
        </div>
        {canEdit && (
          <button
            onClick={() => { setEditing(undefined); setShowForm(true); }}
            className="flex items-center gap-2 bg-red-600 hover:bg-red-700 text-white text-sm px-4 py-2 rounded-lg shadow-sm transition"
          >
            <Plus size={15} /> Block URL
          </button>
        )}
      </div>

      {/* Warning banner */}
      <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3 mb-6">
        <AlertTriangle size={16} className="text-amber-500 mt-0.5 shrink-0" />
        <p className="text-sm text-amber-700">
          URL blocking is enforced by modifying the OS hosts file on each device.
          Agents must have write access to the hosts file (requires admin/sudo privileges on the endpoint).
          Blocked URL visits are always logged regardless of OS-level blocking status.
        </p>
      </div>

      {/* Stats */}
      <div className="flex flex-wrap gap-3 mb-5">
        {[
          { label: "Active Blocks",   value: totalActive,   color: "bg-red-50 text-red-600",       icon: Shield  },
          { label: "Company-wide",    value: companyWide,   color: "bg-orange-50 text-orange-600",  icon: Users2  },
          { label: "Inactive Rules",  value: totalInactive, color: "bg-gray-50 text-gray-600",      icon: Shield  },
        ].map(s => (
          <div key={s.label} className="bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm flex items-center gap-3">
            <div className={`w-8 h-8 rounded-lg ${s.color.split(" ")[0]} flex items-center justify-center`}>
              <s.icon size={14} className={s.color.split(" ")[1]} />
            </div>
            <div>
              <p className="text-xl font-bold text-gray-900 leading-none">{s.value}</p>
              <p className="text-xs text-gray-500 mt-0.5">{s.label}</p>
            </div>
          </div>
        ))}
      </div>

      {/* Filters + Search */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden shrink-0">
          {(["active", "all", "inactive"] as const).map(f => (
            <button key={f} onClick={() => setFilter(f)}
                    className={`px-3 py-1.5 text-sm capitalize transition ${
                      filter === f ? "bg-white text-gray-900 shadow-sm font-medium" : "text-gray-500"
                    }`}>
              {f === "all" ? "All" : f === "active" ? "Active" : "Inactive"}
            </button>
          ))}
        </div>
        <div className="relative flex-1 min-w-[200px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search patterns or notes…"
            className="w-full pl-9 pr-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-red-200 bg-white shadow-sm"
          />
        </div>
      </div>

      {/* Rules table */}
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[720px]">
          <thead>
            <tr className="bg-gray-50 text-gray-400 text-xs uppercase tracking-wide border-b border-gray-200">
              <th className="text-left px-5 py-3 font-semibold">Pattern</th>
              <th className="text-left px-5 py-3 font-semibold">Scope</th>
              <th className="text-left px-5 py-3 font-semibold">Notes</th>
              <th className="text-left px-5 py-3 font-semibold">Status</th>
              {canEdit && <th className="px-5 py-3" />}
            </tr>
          </thead>
          <tbody>
            {filtered.map(rule => (
              <tr key={rule.id} className={`border-t border-gray-100 hover:bg-gray-50 ${!rule.is_active ? "opacity-50" : ""}`}>
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <Globe size={13} className="text-gray-400 shrink-0" />
                    <span className="font-mono text-sm text-gray-800">{rule.pattern}</span>
                  </div>
                </td>
                <td className="px-5 py-3">
                  <TargetBadge rule={rule} devices={devices ?? []} users={users ?? []} />
                </td>
                <td className="px-5 py-3 text-xs text-gray-400 max-w-[200px] truncate">
                  {rule.notes ?? "—"}
                </td>
                <td className="px-5 py-3">
                  {canEdit ? (
                    <button onClick={() => toggleActive(rule)}
                            className="flex items-center gap-1.5 text-xs font-medium transition">
                      {rule.is_active
                        ? <><ToggleRight size={18} className="text-green-500" /><span className="text-green-600">Active</span></>
                        : <><ToggleLeft  size={18} className="text-gray-400"  /><span className="text-gray-500">Inactive</span></>}
                    </button>
                  ) : (
                    <span className={`text-xs font-medium ${rule.is_active ? "text-green-600" : "text-gray-400"}`}>
                      {rule.is_active ? "Active" : "Inactive"}
                    </span>
                  )}
                </td>
                {canEdit && (
                  <td className="px-5 py-3">
                    <div className="flex items-center gap-1 justify-end">
                      <button onClick={() => { setEditing(rule); setShowForm(true); }}
                              className="p-1.5 text-gray-400 hover:text-brand-600 hover:bg-brand-50 rounded-lg transition">
                        <Edit2 size={13} />
                      </button>
                      <button onClick={() => setPendingDeleteId(rule.id)}
                              className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition">
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={canEdit ? 5 : 4} className="px-5 py-12 text-center">
                  <div className="w-12 h-12 rounded-full bg-gray-100 flex items-center justify-center mx-auto mb-3">
                    <Shield size={20} className="text-gray-400" />
                  </div>
                  <p className="text-gray-700 font-medium">No block rules</p>
                  <p className="text-gray-400 text-sm mt-1">
                    {filter === "active" ? 'No active rules. Click "Block URL" to add one.' : "No rules match your search."}
                  </p>
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
      </div>

      <ConfirmDialog
        open={!!pendingDeleteId}
        title="Delete this URL block rule?"
        message="This rule will be removed and the domain will no longer be blocked on agents."
        confirmLabel="Delete Rule"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDeleteId(null)}
      />
      {showForm && (
        <RuleForm
          initial={editing}
          devices={devices ?? []}
          users={users ?? []}
          existingRules={rules ?? []}
          onClose={() => { setShowForm(false); setEditing(undefined); }}
          onSave={() => mutate()}
        />
      )}
    </div>
  );
}
