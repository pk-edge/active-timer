"use client";
import { useState, useMemo } from "react";
import useSWR from "swr";
import { Plus, Pencil, UserX, Search, X, KeyRound } from "lucide-react";
import {
  getUsers, createUser, updateUser, deleteUser, getRoles, getGroups,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { User, Role, Group } from "@/types";

function UserModal({ user, roles, groups, onClose, onSaved }: {
  user: User | null; roles: Role[]; groups: Group[]; onClose: () => void; onSaved: () => void;
}) {
  const [name, setName]     = useState(user?.name ?? "");
  const [email, setEmail]   = useState(user?.email ?? "");
  const [roleId, setRoleId] = useState(user?.role_id ?? "");
  const [password, setPass] = useState("");
  const [groupIds, setGroupIds] = useState<Set<string>>(new Set(user?.group_ids ?? []));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  const selectedRole = roles.find(r => r.id === roleId);
  const showGroups = selectedRole?.scope_type === "groups";

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
                           onChange={() => { const n = new Set(groupIds); n.has(g.id) ? n.delete(g.id) : n.add(g.id); setGroupIds(n); }} />
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

export default function UsersPage() {
  const { loading, can } = useUser();
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<User | null | "new">(null);
  const { data: users, mutate } = useSWR<User[]>("users", () => getUsers());
  const { data: roles }  = useSWR<Role[]>("roles-for-users", getRoles);
  const { data: groups } = useSWR<Group[]>("groups-for-users", getGroups);

  const filtered = useMemo(() => {
    if (!users) return [];
    if (!search.trim()) return users;
    const q = search.toLowerCase();
    return users.filter(u => u.name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q));
  }, [users, search]);

  if (loading) return null;
  if (!can("users", "view")) return <AccessDenied label="Users" />;

  async function onDeactivate(u: User) {
    if (!confirm(`Deactivate ${u.name}?`)) return;
    try { await deleteUser(u.id); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }

  return (
    <div className="overflow-y-auto pr-1">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900">Users</h1>
          <p className="text-gray-500 text-sm mt-0.5">{users?.length ?? 0} people</p>
        </div>
        {can("users", "create") && (
          <button onClick={() => setEditing("new")} className="flex items-center gap-2 bg-blue-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-blue-700">
            <Plus size={15} /> Add user
          </button>
        )}
      </div>

      <div className="relative mb-4 max-w-sm">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search name or email…"
               className="w-full pl-9 pr-9 py-2 bg-white border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-300" />
        {search && <button onClick={() => setSearch("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"><X size={14} /></button>}
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <table className="w-full text-sm">
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
            {filtered.map(u => (
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
                  {can("users", "edit") && <button onClick={() => setEditing(u)} className="text-gray-400 hover:text-blue-600 p-1"><Pencil size={15} /></button>}
                  {can("users", "delete") && u.is_active && <button onClick={() => onDeactivate(u)} className="text-gray-400 hover:text-red-600 p-1"><UserX size={15} /></button>}
                </td>
              </tr>
            ))}
            {filtered.length === 0 && <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400 text-sm">No users found.</td></tr>}
          </tbody>
        </table>
      </div>

      {editing !== null && roles && groups && (
        <UserModal user={editing === "new" ? null : editing} roles={roles} groups={groups}
                   onClose={() => setEditing(null)} onSaved={() => { setEditing(null); mutate(); }} />
      )}
    </div>
  );
}
