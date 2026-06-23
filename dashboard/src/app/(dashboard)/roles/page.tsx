"use client";
import PageLoader from "@/components/PageLoader";
import { useState } from "react";
import useSWR from "swr";
import { Shield, Plus, Pencil, Trash2, X } from "lucide-react";
import { getRoles, getModules, createRole, updateRole, deleteRole } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import ConfirmDialog from "@/components/ConfirmDialog";
import type { Role, ModuleDef, Permission } from "@/types";

const SCOPES = [
  { value: "all",    label: "All data",     hint: "Sees everything in the company" },
  { value: "groups", label: "Assigned groups", hint: "Sees only groups assigned to the user" },
  { value: "own",    label: "Own devices",  hint: "Sees only their own devices" },
];

const TALENT_ROLE = "Talent";

/** System roles are locked except Talent, whose permissions admins may customize. */
function canEditRole(role: Role): boolean {
  return !role.is_system || role.name === TALENT_ROLE;
}

function RoleEditor({ role, modules, onClose, onSaved }: {
  role: Role | null; modules: ModuleDef[]; onClose: () => void; onSaved: () => void;
}) {
  const [name, setName]   = useState(role?.name ?? "");
  const [desc, setDesc]   = useState(role?.description ?? "");
  const [scope, setScope] = useState(role?.scope_type ?? "groups");
  const [perms, setPerms] = useState<Set<string>>(
    new Set((role?.permissions ?? []).map(p => `${p.module}:${p.action}`)));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  const toggle = (m: string, a: string) => {
    const k = `${m}:${a}`;
    const next = new Set(perms);
    next.has(k) ? next.delete(k) : next.add(k);
    setPerms(next);
  };

  async function save() {
    setSaving(true); setErr("");
    const permissions: Permission[] = Array.from(perms).map(k => {
      const [module, action] = k.split(":");
      return { module, action };
    });
    try {
      if (role) await updateRole(role.id, { name, description: desc, scope_type: scope, permissions });
      else      await createRole({ name, description: desc, scope_type: scope, permissions });
      onSaved();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setSaving(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">{role ? "Edit role" : "New role"}</h2>
          <button onClick={onClose} className="text-gray-600 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Role name</label>
              <input value={name} onChange={e => setName(e.target.value)} disabled={role?.is_system}
                     className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Description</label>
              <input value={desc ?? ""} onChange={e => setDesc(e.target.value)}
                     className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1.5">Data scope</label>
            <div className="grid grid-cols-3 gap-2">
              {SCOPES.map(s => (
                <button key={s.value} onClick={() => setScope(s.value)} disabled={role?.is_system}
                        className={`text-left border rounded-lg px-3 py-2 transition disabled:opacity-60 disabled:cursor-not-allowed ${scope === s.value ? "border-brand-400 ring-2 ring-blue-100" : "border-gray-200 hover:border-gray-300"}`}>
                  <p className="text-sm font-medium text-gray-800">{s.label}</p>
                  <p className="text-[11px] text-gray-600">{s.hint}</p>
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1.5">Permissions</label>
            <div className="border border-gray-200 rounded-lg overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm min-w-[560px]">
                <thead>
                  <tr className="bg-gray-50 text-gray-600 text-xs">
                    <th className="text-left px-3 py-2 font-medium">Module</th>
                    {["view", "create", "edit", "delete"].map(a => <th key={a} className="px-2 py-2 font-medium capitalize w-16">{a}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {modules.map(m => (
                    <tr key={m.key} className="border-t border-gray-100">
                      <td className="px-3 py-2 text-gray-700">{m.label}</td>
                      {["view", "create", "edit", "delete"].map(a => (
                        <td key={a} className="text-center">
                          {m.actions.includes(a) ? (
                            <input type="checkbox" checked={perms.has(`${m.key}:${a}`)} onChange={() => toggle(m.key, a)}
                                   className="w-4 h-4 accent-[#4a0f70]" />
                          ) : <span className="text-gray-200">—</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            </div>
          </div>

          {err && <p className="text-red-500 text-sm">{err}</p>}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Cancel</button>
          <button onClick={save} disabled={saving || !name}
                  className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
            {saving ? "Saving…" : "Save role"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function RolesPage() {
  const { loading, can } = useUser();
  const { data: roles, mutate } = useSWR<Role[]>("roles", getRoles);
  const { data: modules } = useSWR<ModuleDef[]>("modules", getModules);
  const [editing,       setEditing]      = useState<Role | null | "new">(null);
  const [pendingDelete, setPendingDelete] = useState<Role | null>(null);
  const [deleting,      setDeleting]      = useState(false);

  if (loading) return <PageLoader />;
  if (!can("roles", "view")) return <AccessDenied label="Roles" />;

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try { await deleteRole(pendingDelete.id); mutate(); setPendingDelete(null); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setDeleting(false); }
  }

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Roles</h1>
          <p className="text-gray-600 text-sm mt-0.5">Define what each role can do and see</p>
        </div>
        {can("roles", "create") && (
          <button onClick={() => setEditing("new")} className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
            <Plus size={15} /> New role
          </button>
        )}
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[560px]">
          <thead>
            <tr className="bg-gray-50 text-gray-600 text-xs uppercase tracking-wide">
              <th className="text-left px-4 py-3 font-semibold">Role</th>
              <th className="text-left px-4 py-3 font-semibold">Scope</th>
              <th className="text-left px-4 py-3 font-semibold">Permissions</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {roles?.map(r => (
              <tr key={r.id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <Shield size={14} className="text-gray-600" />
                    <span className="font-medium text-gray-800">{r.name}</span>
                    {r.is_system && <span className="text-[10px] bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded-full">system</span>}
                  </div>
                  {r.description && <p className="text-xs text-gray-600 mt-0.5 ml-6">{r.description}</p>}
                </td>
                <td className="px-4 py-3 text-gray-600 capitalize">{r.scope_type}</td>
                <td className="px-4 py-3 text-gray-600">{r.permissions.length} permissions</td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  {can("roles", "edit") && canEditRole(r) && (
                    <button onClick={() => setEditing(r)} className="text-gray-600 hover:text-brand-600 p-1"><Pencil size={15} /></button>
                  )}
                  {can("roles", "delete") && !r.is_system && (
                    <button onClick={() => setPendingDelete(r)} className="text-gray-600 hover:text-red-600 p-1"><Trash2 size={15} /></button>
                  )}
                </td>
              </tr>
            ))}
            {roles?.length === 0 && <tr><td colSpan={4} className="px-4 py-10 text-center text-gray-600 text-sm">No roles yet.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>

      {editing !== null && modules && (
        <RoleEditor role={editing === "new" ? null : editing} modules={modules}
                    onClose={() => setEditing(null)}
                    onSaved={() => { setEditing(null); mutate(); }} />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        title={`Delete role "${pendingDelete?.name}"?`}
        message="Users assigned to this role will keep their assignment but the role won't be usable for new assignments."
        confirmLabel="Delete Role"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
