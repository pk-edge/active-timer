"use client";
import PageLoader from "@/components/PageLoader";
import { use, useState, useMemo } from "react";
import Link from "next/link";
import useSWR from "swr";
import { ArrowLeft, UserPlus, X, Trash2, User as UserIcon, Monitor, Search } from "lucide-react";
import { getGroup, getUsers, getDevices, addGroupMember, removeGroupMember } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { GroupDetail, User, Device } from "@/types";

function AddMemberModal({ groupId, existing, onClose, onSaved }: {
  groupId: string; existing: GroupDetail; onClose: () => void; onSaved: () => void;
}) {
  const [tab, setTab] = useState<"employee" | "device">("employee");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState("");
  const { data: users }   = useSWR<User[]>("users-pick", () => getUsers());
  const { data: devices } = useSWR<Device[]>("devices-pick", getDevices);

  const haveEmp = new Set(existing.members.filter(m => m.member_type === "employee").map(m => m.employee_id));
  const haveDev = new Set(existing.members.filter(m => m.member_type === "device").map(m => m.device_id));

  const empList = useMemo(() => (users ?? []).filter(u =>
    !haveEmp.has(u.id) && (!q || u.name.toLowerCase().includes(q.toLowerCase()) || u.email.toLowerCase().includes(q.toLowerCase()))), [users, q]);
  const devList = useMemo(() => (devices ?? []).filter(d =>
    !haveDev.has(d.id) && (!q || d.hostname.toLowerCase().includes(q.toLowerCase()))), [devices, q]);

  async function add(type: "employee" | "device", id: string) {
    setBusy(id);
    try {
      await addGroupMember(groupId, type === "employee" ? { member_type: "employee", employee_id: id } : { member_type: "device", device_id: id });
      onSaved();
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(""); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md max-h-[85vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">Add member</h2>
          <button onClick={onClose} className="text-gray-600 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="flex border-b border-gray-100">
          {(["employee", "device"] as const).map(t => (
            <button key={t} onClick={() => setTab(t)}
                    className={`flex-1 py-2.5 text-sm font-medium capitalize transition ${tab === t ? "text-brand-600 border-b-2 border-brand-600" : "text-gray-600"}`}>
              {t === "employee" ? "Users" : "Devices"}
            </button>
          ))}
        </div>
        <div className="p-4">
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search…"
                   className="w-full pl-9 pr-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-1">
          {tab === "employee" ? (
            empList.length === 0 ? <p className="text-sm text-gray-600 text-center py-6">No users to add.</p> :
            empList.map(u => (
              <button key={u.id} onClick={() => add("employee", u.id)} disabled={busy === u.id}
                      className="flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg hover:bg-gray-50 disabled:opacity-50">
                <UserIcon size={14} className="text-gray-600" />
                <span className="text-sm text-gray-800 flex-1 truncate">{u.name}</span>
                <span className="text-xs text-gray-600 truncate">{u.email}</span>
              </button>
            ))
          ) : (
            devList.length === 0 ? <p className="text-sm text-gray-600 text-center py-6">No devices to add.</p> :
            devList.map(d => (
              <button key={d.id} onClick={() => add("device", d.id)} disabled={busy === d.id}
                      className="flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg hover:bg-gray-50 disabled:opacity-50">
                <Monitor size={14} className="text-gray-600" />
                <span className="text-sm text-gray-800 flex-1 truncate">{d.hostname}</span>
                <span className="text-xs text-gray-600">{d.employee_name ?? d.platform}</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

export default function GroupDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { loading, can } = useUser();
  const { data: group, mutate } = useSWR<GroupDetail>(["group", id], () => getGroup(id));
  const [adding, setAdding] = useState(false);

  if (loading) return <PageLoader />;
  if (!can("groups", "view")) return <AccessDenied label="Groups" />;

  async function removeMember(memberId: string) {
    try { await removeGroupMember(id, memberId); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }

  return (
    <div className="overflow-y-auto pr-1">
      <Link href="/groups" className="inline-flex items-center gap-1.5 text-gray-600 text-sm mb-3 hover:text-gray-700">
        <ArrowLeft size={13} /> All groups
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">{group?.name ?? "…"}</h1>
          <p className="text-gray-600 text-sm mt-0.5">{group?.member_count ?? 0} members{group?.description ? ` · ${group.description}` : ""}</p>
        </div>
        {can("groups", "edit") && group && (
          <button onClick={() => setAdding(true)} className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
            <UserPlus size={15} /> Add member
          </button>
        )}
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[600px]">
          <thead>
            <tr className="bg-gray-50 text-gray-600 text-xs uppercase tracking-wide">
              <th className="text-left px-4 py-3 font-semibold">Member</th>
              <th className="text-left px-4 py-3 font-semibold">Type</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {group?.members.map(m => (
              <tr key={m.id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    {m.member_type === "employee" ? <UserIcon size={14} className="text-gray-600" /> : <Monitor size={14} className="text-gray-600" />}
                    <span className="font-medium text-gray-800">{m.label}</span>
                  </div>
                </td>
                <td className="px-4 py-3 capitalize text-gray-600">{m.member_type}</td>
                <td className="px-4 py-3 text-right">
                  {can("groups", "edit") && <button onClick={() => removeMember(m.id)} className="text-gray-600 hover:text-red-600 p-1"><Trash2 size={15} /></button>}
                </td>
              </tr>
            ))}
            {group?.members.length === 0 && <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-600 text-sm">No members yet. Add users or devices.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>

      {adding && group && <AddMemberModal groupId={id} existing={group} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); mutate(); }} />}
    </div>
  );
}
