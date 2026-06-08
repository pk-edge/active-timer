"use client";
import { useState, useMemo, useEffect, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";
import { Plus, Pencil, UserX, Search, X, UserCheck, Trash2, UserCheck2 } from "lucide-react";
import {
  getUsers, getPendingUsers, createUser, updateUser, deleteUser,
  deactivateUser, activateUser, approveUser, getRoles, getGroups,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { User, Role, Group } from "@/types";

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
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">{user ? "Edit user" : "Add user"}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Name</label>
            <input value={name} onChange={e => setName(e.target.value)}
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-300" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Email</label>
            <input value={email} onChange={e => setEmail(e.target.value)} disabled={!!user} type="email"
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm disabled:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-300" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Role (login access)</label>
            <select value={roleId} onChange={e => setRoleId(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-300">
              <option value="">No dashboard login (monitored only)</option>
              {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              {user ? "Reset password (optional)" : "Password (optional)"}
            </label>
            <input value={password} onChange={e => setPass(e.target.value)} type="password" placeholder="••••••••"
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-300" />
          </div>
          {showGroups && (
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Groups this user can view</label>
              <div className="max-h-36 overflow-y-auto border border-gray-200 rounded-lg p-2 space-y-1">
                {groups.length === 0 && <p className="text-xs text-gray-400 px-1">No groups yet.</p>}
                {groups.map(g => (
                  <label key={g.id} className="flex items-center gap-2 text-sm px-1 py-0.5 cursor-pointer">
                    <input type="checkbox" className="accent-blue-600" checked={groupIds.has(g.id)}
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
          <button onClick={save} disabled={saving || !name || (!user && !email)}
                  className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">
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
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-300">
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
                    <input type="checkbox" className="accent-blue-600" checked={groupIds.has(g.id)}
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

  const filteredActive = useMemo(() => {
    if (!users) return [];
    if (!search.trim()) return users;
    const q = search.toLowerCase();
    return users.filter(u => u.name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q));
  }, [users, search]);

  function refresh() { mutateUsers(); mutatePending(); }

  if (loading) return null;
  if (!can("users", "view")) return <AccessDenied label="Users" />;

  async function onDeactivate(u: User) {
    if (!confirm(`Deactivate ${u.name}? They will lose dashboard access but remain in the system.`)) return;
    try { await deactivateUser(u.id); refresh(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }
  async function onActivate(u: User) {
    if (!confirm(`Re-activate ${u.name}?`)) return;
    try { await activateUser(u.id); refresh(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }
  async function onDelete(u: User) {
    if (!confirm(`Delete ${u.name} permanently? This cannot be undone.`)) return;
    try { await deleteUser(u.id); refresh(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }
  async function onReject(u: User) {
    if (!confirm(`Reject and delete the registration for ${u.name}?`)) return;
    try { await deleteUser(u.id); refresh(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }

  const pendingCount = pending?.length ?? 0;

  return (
    <div className="overflow-y-auto pr-1">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Users</h1>
          <p className="text-gray-500 text-sm mt-0.5">{users?.length ?? 0} active</p>
        </div>
        {tab === "active" && can("users", "create") && (
          <button onClick={() => setEditing("new")}
                  className="flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-blue-700">
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
                   className="w-full pl-9 pr-9 py-2 bg-white border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-300" />
            {search && <button onClick={() => setSearch("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"><X size={14} /></button>}
          </div>
        )}
      </div>

      {/* Active users table */}
      {tab === "active" && (
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-[720px]">
            <thead>
              <tr className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                <th className="text-left px-4 py-3 font-semibold">Name</th>
                <th className="text-left px-4 py-3 font-semibold">Email</th>
                <th className="text-left px-4 py-3 font-semibold">Role</th>
                <th className="text-left px-4 py-3 font-semibold">Devices</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {filteredActive.map(u => (
                <tr key={u.id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-gray-800">{u.name}</td>
                  <td className="px-4 py-3 text-gray-500">{u.email}</td>
                  <td className="px-4 py-3">
                    {u.role_name
                      ? <span className="text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full">{u.role_name}</span>
                      : <span className="text-xs text-gray-400">monitored only</span>}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{u.device_count}</td>
                  <td className="px-4 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded-full ${u.is_active ? "bg-green-50 text-green-600" : "bg-gray-100 text-gray-500"}`}>
                      {u.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    {can("users", "edit") && (
                      <button onClick={() => setEditing(u)} className="text-gray-400 hover:text-blue-600 p-1" title="Edit">
                        <Pencil size={15} />
                      </button>
                    )}
                    {can("users", "edit") && u.is_active && (
                      <button onClick={() => onDeactivate(u)} className="text-gray-400 hover:text-amber-600 p-1" title="Deactivate (temporary)">
                        <UserX size={15} />
                      </button>
                    )}
                    {can("users", "edit") && !u.is_active && (
                      <button onClick={() => onActivate(u)} className="text-gray-400 hover:text-green-600 p-1" title="Re-activate">
                        <UserCheck2 size={15} />
                      </button>
                    )}
                    {can("users", "delete") && (
                      <button onClick={() => onDelete(u)} className="text-gray-400 hover:text-red-600 p-1" title="Delete permanently">
                        <Trash2 size={15} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {filteredActive.length === 0 && <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400 text-sm">No users found.</td></tr>}
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
                        <button onClick={() => onReject(u)} className="text-gray-400 hover:text-red-600 p-1"><UserX size={15} /></button>
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
