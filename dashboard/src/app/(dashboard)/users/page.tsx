"use client";
import { useState, useMemo, useEffect, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import useSWR from "swr";
import { Plus, Pencil, UserX, Search, X, UserCheck, Trash2, UserCheck2, Monitor } from "lucide-react";
import {
  getUsers, getPendingUsers, createUser, updateUser, deleteUser,
  deactivateUser, activateUser, approveUser, getRoles, getGroups,
  getDevices, assignDevice, getUserActivity, fmtSeconds,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import ConfirmDialog from "@/components/ConfirmDialog";
import type { User, UserActivity, Role, Group, Device } from "@/types";

// ── Create / Edit modal ───────────────────────────────────────────────────────

function UserModal({ user, roles, groups, onClose, onSaved }: {
  user: User | null; roles: Role[]; groups: Group[];
  onClose: () => void; onSaved: () => void;
}) {
  const [name,     setName]     = useState(user?.name ?? "");
  const [email,    setEmail]    = useState(user?.email ?? "");
  const [roleId,   setRoleId]   = useState(user?.role_id ?? "");
  const [password, setPass]     = useState("");
  const [groupIds, setGroupIds] = useState<Set<string>>(new Set(user?.group_ids ?? []));
  const [saving,   setSaving]   = useState(false);
  const [err,      setErr]      = useState("");

  const selectedRole = roles.find(r => r.id === roleId);
  const showGroups   = selectedRole?.scope_type === "groups";

  // Devices panel — existing users only; attach/unlink needs devices.assign.
  const { can } = useUser();
  const showDevices = !!user && can("devices", "view");
  const canAssign   = can("devices", "assign");
  const [deviceBusy, setDeviceBusy] = useState(false);

  const { data: allDevices, mutate: mutateDevices } = useSWR<Device[]>(
    showDevices ? "devices" : null, getDevices,
  );
  const myDevices  = (allDevices ?? []).filter(d => d.employee_id === user?.id);
  const unassigned = (allDevices ?? []).filter(d => !d.employee_id);

  async function setDeviceOwner(deviceId: string, owner: string | null) {
    setDeviceBusy(true);
    try {
      await assignDevice(deviceId, owner);
      await mutateDevices();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setDeviceBusy(false); }
  }

  async function save() {
    setSaving(true); setErr("");
    const body: Record<string, unknown> = {
      name, role_id: roleId || null, group_ids: Array.from(groupIds),
    };
    if (password) body.password = password;
    try {
      if (user) await updateUser(user.id, body);
      else      await createUser({ ...body, email });
      onSaved();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setSaving(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">{user ? "Edit user" : "Add user"}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-3 overflow-y-auto">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Name</label>
            <input value={name} onChange={e => setName(e.target.value)}
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Email</label>
            <input value={email} onChange={e => setEmail(e.target.value)} disabled={!!user} type="email"
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm disabled:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Role (login access)</label>
            <select value={roleId} onChange={e => setRoleId(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300">
              <option value="">No dashboard login (monitored only)</option>
              {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {user ? "Reset password (optional)" : "Password (optional)"}
            </label>
            <input value={password} onChange={e => setPass(e.target.value)} type="password" placeholder="••••••••"
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
          {showGroups && (
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Groups this user can view</label>
              <div className="max-h-36 overflow-y-auto border border-gray-200 rounded-lg p-2 space-y-1">
                {groups.length === 0 && <p className="text-xs text-gray-400 px-1">No groups yet.</p>}
                {groups.map(g => (
                  <label key={g.id} className="flex items-center gap-2 text-sm px-1 py-0.5 cursor-pointer">
                    <input type="checkbox" className="accent-[#4a0f70]" checked={groupIds.has(g.id)}
                           onChange={() => {
                             const n = new Set(groupIds);
                             n.has(g.id) ? n.delete(g.id) : n.add(g.id);
                             setGroupIds(n);
                           }} />
                    {g.name}
                  </label>
                ))}
              </div>
            </div>
          )}
          {showDevices && (
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">
                Devices ({myDevices.length})
              </label>
              <div className="border border-gray-200 rounded-lg divide-y divide-gray-100">
                {myDevices.length === 0 && (
                  <p className="text-xs text-gray-400 px-3 py-2.5">No devices linked yet.</p>
                )}
                {myDevices.map(d => (
                  <div key={d.id} className="flex items-center gap-2 px-3 py-2">
                    <Monitor size={13} className="text-gray-400 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-gray-800 truncate">{d.hostname}</p>
                      <p className="text-[11px] text-gray-400 capitalize">
                        {d.platform} · {d.is_online ? "online" : "offline"}
                      </p>
                    </div>
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${d.is_online ? "bg-green-500" : "bg-gray-300"}`} />
                    {canAssign && (
                      <button onClick={() => setDeviceOwner(d.id, null)} disabled={deviceBusy}
                              className="text-xs text-gray-400 hover:text-red-600 disabled:opacity-50 shrink-0"
                              title="Unlink device">
                        Unlink
                      </button>
                    )}
                  </div>
                ))}
                {canAssign && unassigned.length > 0 && (
                  <div className="px-3 py-2">
                    <select value="" disabled={deviceBusy}
                            onChange={e => e.target.value && setDeviceOwner(e.target.value, user!.id)}
                            className="w-full text-xs border border-gray-200 rounded-lg px-2 py-1.5 text-gray-600 bg-white focus:outline-none focus:ring-2 focus:ring-brand-300 disabled:opacity-50">
                      <option value="">+ Attach an unassigned device…</option>
                      {unassigned.map(d => (
                        <option key={d.id} value={d.id}>{d.hostname} ({d.platform})</option>
                      ))}
                    </select>
                  </div>
                )}
              </div>
            </div>
          )}
          {err && <p className="text-red-500 text-sm">{err}</p>}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Cancel</button>
          <button onClick={save} disabled={saving || !name || (!user && !email)}
                  className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Approve modal ─────────────────────────────────────────────────────────────

function ApproveModal({ user: pending, roles, groups, onClose, onSaved }: {
  user: User; roles: Role[]; groups: Group[];
  onClose: () => void; onSaved: () => void;
}) {
  const [roleId,   setRoleId]   = useState("");
  const [groupIds, setGroupIds] = useState<Set<string>>(new Set());
  const [saving,   setSaving]   = useState(false);
  const [err,      setErr]      = useState("");

  const selectedRole = roles.find(r => r.id === roleId);
  const showGroups   = selectedRole?.scope_type === "groups";

  async function approve() {
    if (!roleId) { setErr("Please assign a role before approving."); return; }
    setSaving(true); setErr("");
    try {
      await approveUser(pending.id, { role_id: roleId, group_ids: Array.from(groupIds) });
      onSaved();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setSaving(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">Approve registration</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-3">
          <p className="text-sm text-gray-600">
            Approving <span className="font-medium text-gray-900">{pending.name}</span>{" "}
            <span className="text-gray-400">({pending.email})</span> — assign a role to grant access.
          </p>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Role <span className="text-red-400">*</span></label>
            <select value={roleId} onChange={e => setRoleId(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300">
              <option value="">Select a role…</option>
              {roles.map(r => <option key={r.id} value={r.id}>{r.name} ({r.scope_type})</option>)}
            </select>
          </div>
          {showGroups && (
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Groups this user can view</label>
              <div className="max-h-36 overflow-y-auto border border-gray-200 rounded-lg p-2 space-y-1">
                {groups.length === 0 && <p className="text-xs text-gray-400 px-1">No groups yet.</p>}
                {groups.map(g => (
                  <label key={g.id} className="flex items-center gap-2 text-sm px-1 py-0.5 cursor-pointer">
                    <input type="checkbox" className="accent-[#4a0f70]" checked={groupIds.has(g.id)}
                           onChange={() => {
                             const n = new Set(groupIds);
                             n.has(g.id) ? n.delete(g.id) : n.add(g.id);
                             setGroupIds(n);
                           }} />
                    {g.name}
                  </label>
                ))}
              </div>
            </div>
          )}
          {err && <p className="text-red-500 text-sm">{err}</p>}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Cancel</button>
          <button onClick={approve} disabled={saving || !roleId}
                  className="flex items-center gap-1.5 px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50">
            <UserCheck size={14} /> {saving ? "Approving…" : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

type Tab = "active" | "pending";

function UsersPageInner() {
  const { loading, can } = useUser();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<Tab>(() =>
    searchParams.get("tab") === "pending" ? "pending" : "active"
  );

  useEffect(() => {
    if (searchParams.get("tab") === "pending") setTab("pending");
  }, [searchParams]);
  const [search,  setSearch]  = useState("");
  const [editing, setEditing] = useState<User | null | "new">(null);
  const [approving, setApproving] = useState<User | null>(null);

  const { data: users,   mutate: mutateUsers }   = useSWR<User[]>("users",         () => getUsers());
  const { data: pending, mutate: mutatePending }  = useSWR<User[]>("users-pending", getPendingUsers);
  const { data: roles }  = useSWR<Role[]>("roles-for-users",  getRoles);
  const { data: groups } = useSWR<Group[]>("groups-for-users", getGroups);

  // One batched call for ALL users' live status + time spent; refresh keeps the
  // online dots current without per-user requests.
  const { data: activity } = useSWR<UserActivity[]>("users-activity", getUserActivity, {
    refreshInterval: 30_000,
  });
  const actById = useMemo(
    () => new Map((activity ?? []).map(a => [a.id, a])),
    [activity],
  );

  const filteredActive = useMemo(() => {
    if (!users) return [];
    if (!search.trim()) return users;
    const q = search.toLowerCase();
    return users.filter(u => u.name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q));
  }, [users, search]);

  function refresh() { mutateUsers(); mutatePending(); }

  // Confirm dialog state — one dialog handles deactivate / delete / reject
  type ActionKind = "deactivate" | "activate" | "delete" | "reject";
  const [pendingAction, setPendingAction] = useState<{ user: User; kind: ActionKind } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const CONFIRM_COPY: Record<ActionKind, { title: string; message: string; label: string; variant: "danger" | "warning" }> = {
    deactivate: { title: "Deactivate user?", message: "They will lose dashboard access but remain in the system.",   label: "Deactivate", variant: "warning" },
    activate:   { title: "Re-activate user?", message: "User will regain their previous access level.",               label: "Activate",   variant: "warning" },
    delete:     { title: "Delete user?",      message: "This user will be permanently removed from the system.",      label: "Delete",     variant: "danger"  },
    reject:     { title: "Reject registration?", message: "The registration request will be permanently deleted.",   label: "Reject",     variant: "danger"  },
  };

  async function confirmAction() {
    if (!pendingAction) return;
    setActionLoading(true);
    const { user: u, kind } = pendingAction;
    try {
      if (kind === "deactivate") await deactivateUser(u.id);
      else if (kind === "activate") await activateUser(u.id);
      else await deleteUser(u.id);
      refresh();
      setPendingAction(null);
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setActionLoading(false); }
  }

  if (loading) return null;
  if (!can("users", "view")) return <AccessDenied label="Users" />;

  const pendingCount = pending?.length ?? 0;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Users</h1>
          <p className="text-gray-500 text-sm mt-0.5">{users?.length ?? 0} active</p>
        </div>
        {tab === "active" && can("users", "create") && (
          <button onClick={() => setEditing("new")}
                  className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
            <Plus size={15} /> Add user
          </button>
        )}
      </div>

      {/* Tabs + search */}
      <div className="flex items-center gap-4 mb-4">
        <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden">
          <button onClick={() => setTab("active")}
                  className={`px-3 py-1.5 text-sm font-medium transition ${tab === "active" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500"}`}>
            Active
          </button>
          <button onClick={() => setTab("pending")}
                  className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium transition ${tab === "pending" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500"}`}>
            Pending
            {pendingCount > 0 && (
              <span className="bg-amber-100 text-amber-700 text-xs font-semibold px-1.5 py-0.5 rounded-full">{pendingCount}</span>
            )}
          </button>
        </div>

        {tab === "active" && (
          <div className="relative max-w-xs flex-1">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search name or email…"
                   className="w-full pl-9 pr-9 py-2 bg-white border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
            {search && <button onClick={() => setSearch("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"><X size={14} /></button>}
          </div>
        )}
      </div>

      {/* Active users table */}
      {tab === "active" && (
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-[920px]">
            <thead>
              <tr className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                <th className="text-left px-4 py-3 font-semibold">Name</th>
                <th className="text-left px-4 py-3 font-semibold">Role</th>
                <th className="text-left px-4 py-3 font-semibold">Online</th>
                <th className="text-left px-4 py-3 font-semibold">Time today</th>
                <th className="text-left px-4 py-3 font-semibold">Last 7 days</th>
                <th className="text-left px-4 py-3 font-semibold">Devices</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {filteredActive.map(u => {
                const act = actById.get(u.id);
                return (
                <tr
                  key={u.id}
                  onClick={() => router.push(`/users/${u.id}`)}
                  className="border-t border-gray-100 hover:bg-gray-50 cursor-pointer"
                  title="View activity"
                >
                  <td className="px-4 py-3">
                    <p className="font-medium text-gray-800">{u.name}</p>
                    <p className="text-xs text-gray-400">{u.email}</p>
                  </td>
                  <td className="px-4 py-3">
                    {u.role_name
                      ? <span className="text-xs bg-brand-50 text-brand-700 px-2 py-0.5 rounded-full">{u.role_name}</span>
                      : <span className="text-xs text-gray-400">monitored only</span>}
                  </td>
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-1.5 text-xs">
                      <span className={`w-2 h-2 rounded-full shrink-0 ${act?.is_online ? "bg-green-500" : "bg-gray-300"}`} />
                      <span className={act?.is_online ? "text-green-600 font-medium" : "text-gray-400"}>
                        {act?.is_online ? "Online" : "Offline"}
                      </span>
                    </span>
                    {act?.is_online && act.current_app && (
                      <p className="text-[11px] text-gray-400 mt-0.5 truncate max-w-[140px]" title={act.current_app}>
                        {act.current_app}
                      </p>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-700 font-medium tabular-nums">
                    {act ? fmtSeconds(act.today_seconds) : "—"}
                  </td>
                  <td className="px-4 py-3 text-gray-500 tabular-nums">
                    {act ? fmtSeconds(act.week_seconds) : "—"}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{u.device_count}</td>
                  <td className="px-4 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded-full ${u.is_active ? "bg-green-50 text-green-600" : "bg-gray-100 text-gray-500"}`}>
                      {u.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap" onClick={e => e.stopPropagation()}>
                    {can("users", "edit") && (
                      <button onClick={() => setEditing(u)} className="text-gray-400 hover:text-brand-600 p-1" title="Edit">
                        <Pencil size={15} />
                      </button>
                    )}
                    {can("users", "edit") && u.is_active && (
                      <button onClick={() => setPendingAction({ user: u, kind: 'deactivate' })} className="text-gray-400 hover:text-amber-600 p-1" title="Deactivate (temporary)">
                        <UserX size={15} />
                      </button>
                    )}
                    {can("users", "edit") && !u.is_active && (
                      <button onClick={() => setPendingAction({ user: u, kind: 'activate' })} className="text-gray-400 hover:text-green-600 p-1" title="Re-activate">
                        <UserCheck2 size={15} />
                      </button>
                    )}
                    {can("users", "delete") && (
                      <button onClick={() => setPendingAction({ user: u, kind: 'delete' })} className="text-gray-400 hover:text-red-600 p-1" title="Delete permanently">
                        <Trash2 size={15} />
                      </button>
                    )}
                  </td>
                </tr>
                );
              })}
              {filteredActive.length === 0 && <tr><td colSpan={8} className="px-4 py-10 text-center text-gray-400 text-sm">No users found.</td></tr>}
            </tbody>
          </table>
          </div>
        </div>
      )}

      {/* Pending registrations table */}
      {tab === "pending" && (
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          {pendingCount === 0 ? (
            <div className="px-4 py-12 text-center">
              <UserCheck size={28} className="mx-auto mb-2 text-gray-200" />
              <p className="text-sm text-gray-400">No pending registrations.</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm min-w-[720px]">
              <thead>
                <tr className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                  <th className="text-left px-4 py-3 font-semibold">Name</th>
                  <th className="text-left px-4 py-3 font-semibold">Email</th>
                  <th className="text-left px-4 py-3 font-semibold">Requested</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody>
                {pending?.map(u => (
                  <tr key={u.id} className="border-t border-gray-100 hover:bg-gray-50">
                    <td className="px-4 py-3 font-medium text-gray-800">{u.name}</td>
                    <td className="px-4 py-3 text-gray-500">{u.email}</td>
                    <td className="px-4 py-3 text-gray-400 text-xs">
                      {u.device_count === 0 ? "—" : u.device_count}
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      {can("users", "edit") && (
                        <button onClick={() => setApproving(u)}
                                className="flex items-center gap-1 text-xs bg-green-50 text-green-700 hover:bg-green-100 px-2.5 py-1 rounded-full font-medium transition inline-flex mr-2">
                          <UserCheck size={12} /> Approve
                        </button>
                      )}
                      {can("users", "delete") && (
                        <button onClick={() => setPendingAction({ user: u, kind: 'reject' })} className="text-gray-400 hover:text-red-600 p-1"><UserX size={15} /></button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </div>
      )}

      {/* Modals */}
      {editing !== null && roles && groups && (
        <UserModal user={editing === "new" ? null : editing} roles={roles} groups={groups}
                   onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} />
      )}
      {approving && roles && groups && (
        <ApproveModal user={approving} roles={roles} groups={groups}
                      onClose={() => setApproving(null)} onSaved={() => { setApproving(null); refresh(); }} />
      )}

      {pendingAction && (
        <ConfirmDialog
          open
          title={`${CONFIRM_COPY[pendingAction.kind].title} — ${pendingAction.user.name}`}
          message={CONFIRM_COPY[pendingAction.kind].message}
          detail={`User: ${pendingAction.user.email}`}
          confirmLabel={CONFIRM_COPY[pendingAction.kind].label}
          variant={CONFIRM_COPY[pendingAction.kind].variant}
          loading={actionLoading}
          onConfirm={confirmAction}
          onCancel={() => setPendingAction(null)}
        />
      )}
    </div>
  );
}

export default function UsersPage() {
  return (
    <Suspense fallback={null}>
      <UsersPageInner />
    </Suspense>
  );
}
