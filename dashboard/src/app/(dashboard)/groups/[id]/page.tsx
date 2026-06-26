"use client";
import PageLoader from "@/components/PageLoader";
import { use, useState, useMemo } from "react";
import Link from "next/link";
import useSWR from "swr";
import {
  ArrowLeft, UserPlus, X, Trash2, User as UserIcon, Monitor, Search,
  Plus, Pencil, ChevronRight, FolderKanban, Archive, RotateCcw,
} from "lucide-react";
import GroupPicker from "@/components/GroupPicker";
import {
  getGroup, getUsers, getDevices, addGroupMember, removeGroupMember,
  updateGroup, createGroup, deleteGroup, archiveGroup, unarchiveGroup,
  getGroupMembers,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import type { GroupDetail, GroupMember, User, Device } from "@/types";

function EditGroupModal({ group, onClose, onSaved }: {
  group: GroupDetail; onClose: () => void; onSaved: () => void;
}) {
  const [name, setName] = useState(group.name);
  const [desc, setDesc] = useState(group.description ?? "");
  const [parentId, setParentId] = useState(group.parent_id ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    setBusy(true); setErr("");
    try {
      await updateGroup(group.id, { name, description: desc, parent_id: parentId || null });
      onSaved();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">Edit group</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Parent group</label>
            <GroupPicker value={parentId} onChange={setParentId} excludeId={group.id} allowEmpty emptyLabel="Top-level group" className="w-full" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Name</label>
            <input value={name} onChange={e => setName(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Description</label>
            <input value={desc} onChange={e => setDesc(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
          {err && <p className="text-red-500 text-sm">{err}</p>}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600">Cancel</button>
          <button onClick={save} disabled={busy || !name}
            className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Self-sufficient: fetches its own member list so it can be used for any group/subgroup.
function AddMemberModal({ groupId, onClose, onSaved }: {
  groupId: string; onClose: () => void; onSaved: () => void;
}) {
  const [tab, setTab] = useState<"employee" | "device">("employee");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState("");
  const { data: members } = useSWR<GroupMember[]>(["group-members", groupId], () => getGroupMembers(groupId));
  const { data: users }   = useSWR<User[]>("users-pick", () => getUsers());
  const { data: devices } = useSWR<Device[]>("devices-pick", () => getDevices());

  const haveEmp = useMemo(
    () => new Set((members ?? []).filter(m => m.member_type === "employee").map(m => m.employee_id)),
    [members]
  );
  const haveDev = useMemo(
    () => new Set((members ?? []).filter(m => m.member_type === "device").map(m => m.device_id)),
    [members]
  );

  const empList = useMemo(() => (users ?? []).filter(u =>
    !q || u.name.toLowerCase().includes(q.toLowerCase()) || u.email.toLowerCase().includes(q.toLowerCase())
  ), [users, q]);

  const devList = useMemo(() => (devices ?? []).filter(d =>
    !q || d.hostname.toLowerCase().includes(q.toLowerCase())
  ), [devices, q]);

  async function add(type: "employee" | "device", id: string) {
    setBusy(id);
    try {
      await addGroupMember(groupId, type === "employee"
        ? { member_type: "employee", employee_id: id }
        : { member_type: "device", device_id: id });
      onSaved();
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(""); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md flex flex-col" style={{ maxHeight: "85vh" }}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 shrink-0">
          <h2 className="font-semibold text-gray-900">Add member</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="flex border-b border-gray-100 shrink-0">
          {(["employee", "device"] as const).map(t => (
            <button key={t} onClick={() => { setTab(t); setQ(""); }}
              className={`flex-1 py-2.5 text-sm font-medium capitalize transition ${tab === t ? "text-brand-600 border-b-2 border-brand-600" : "text-gray-500"}`}>
              {t === "employee" ? "Users" : "Devices"}
            </button>
          ))}
        </div>
        <div className="p-4 shrink-0">
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search…" autoFocus
              className="w-full pl-9 pr-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-4 pb-2 space-y-1 min-h-0">
          {tab === "employee" ? (
            empList.length === 0
              ? <p className="text-sm text-gray-500 text-center py-6">No users found.</p>
              : empList.map(u => {
                const inGroup = haveEmp.has(u.id);
                const otherGroups = (u.group_ids ?? []).filter(gid => gid !== groupId).length;
                return (
                  <button key={u.id}
                    onClick={() => !inGroup && add("employee", u.id)}
                    disabled={busy === u.id || inGroup}
                    className={`flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg transition ${inGroup ? "opacity-50 cursor-default" : "hover:bg-gray-50"} disabled:opacity-50`}>
                    <UserIcon size={14} className="text-gray-400 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-gray-800 truncate">{u.name}</p>
                      {inGroup
                        ? <p className="text-xs text-red-400">Already in this group</p>
                        : otherGroups > 0
                          ? <p className="text-xs text-amber-500">Already in {otherGroups} other group{otherGroups !== 1 ? "s" : ""}</p>
                          : null}
                    </div>
                    <span className="text-xs text-gray-400 truncate shrink-0 max-w-[120px]">{u.email}</span>
                    {busy === u.id && <span className="text-xs text-gray-400 shrink-0">Adding…</span>}
                  </button>
                );
              })
          ) : (
            devList.length === 0
              ? <p className="text-sm text-gray-500 text-center py-6">No devices found.</p>
              : devList.map(d => {
                const inGroup = haveDev.has(d.id);
                return (
                  <button key={d.id}
                    onClick={() => !inGroup && add("device", d.id)}
                    disabled={busy === d.id || inGroup}
                    className={`flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg transition ${inGroup ? "opacity-50 cursor-default" : "hover:bg-gray-50"} disabled:opacity-50`}>
                    <Monitor size={14} className="text-gray-400 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-gray-800 truncate">{d.hostname}</p>
                      {inGroup && <p className="text-xs text-red-400">Already in this group</p>}
                    </div>
                    <span className="text-xs text-gray-500 shrink-0">{d.employee_name ?? d.platform}</span>
                    {busy === d.id && <span className="text-xs text-gray-400 shrink-0">Adding…</span>}
                  </button>
                );
              })
          )}
        </div>
        <div className="flex justify-end px-5 py-4 border-t border-gray-100 shrink-0">
          <button onClick={onClose}
            className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700">
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

function SubgroupCreateModal({ parentGroupId, parentGroupName, onClose, onSaved }: {
  parentGroupId: string;
  parentGroupName: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const [createdGroupId, setCreatedGroupId] = useState<string | null>(null);
  const [createdGroupName, setCreatedGroupName] = useState("");
  const [memberTab, setMemberTab] = useState<"employee" | "device">("employee");
  const [q, setQ] = useState("");
  const [addedIds, setAddedIds] = useState<Set<string>>(new Set());
  const [addingId, setAddingId] = useState("");

  const { data: users } = useSWR<User[]>("users-pick", () => getUsers());
  const { data: devices } = useSWR<Device[]>("devices-pick", () => getDevices());

  const empList = useMemo(() => (users ?? []).filter(u =>
    !q || u.name.toLowerCase().includes(q.toLowerCase()) || u.email.toLowerCase().includes(q.toLowerCase())
  ), [users, q]);

  const devList = useMemo(() => (devices ?? []).filter(d =>
    !q || d.hostname.toLowerCase().includes(q.toLowerCase())
  ), [devices, q]);

  async function createSub() {
    if (!name.trim()) return;
    setBusy(true); setErr("");
    try {
      const group = await createGroup({
        name: name.trim(),
        description: desc || undefined,
        parent_id: parentGroupId,
      });
      setCreatedGroupId(group.id);
      setCreatedGroupName(group.name);
      onSaved();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }

  async function addMember(type: "employee" | "device", id: string) {
    if (!createdGroupId || addingId) return;
    setAddingId(id);
    try {
      await addGroupMember(createdGroupId, type === "employee"
        ? { member_type: "employee", employee_id: id }
        : { member_type: "device", device_id: id });
      setAddedIds(prev => new Set(prev).add(id));
    } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setAddingId(""); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md flex flex-col" style={{ maxHeight: "90vh" }}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 shrink-0">
          <div>
            <h2 className="font-semibold text-gray-900">
              {createdGroupId ? "Add members" : "Create subgroup"}
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {createdGroupId
                ? `"${createdGroupName}" created — add members or click Done`
                : `Under: ${parentGroupName}`}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700"><X size={18} /></button>
        </div>

        {!createdGroupId ? (
          <>
            <div className="p-5 space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Name</label>
                <input value={name} onChange={e => setName(e.target.value)} autoFocus
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300"
                  placeholder="Subgroup name" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Description (optional)</label>
                <input value={desc} onChange={e => setDesc(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
              </div>
              {err && <p className="text-red-500 text-sm">{err}</p>}
            </div>
            <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100 shrink-0">
              <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Cancel</button>
              <button onClick={createSub} disabled={busy || !name.trim()}
                className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
                {busy ? "Creating…" : "Create"}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="flex border-b border-gray-100 shrink-0">
              {(["employee", "device"] as const).map(t => (
                <button key={t} onClick={() => { setMemberTab(t); setQ(""); }}
                  className={`flex-1 py-2.5 text-sm font-medium capitalize transition ${memberTab === t ? "text-brand-600 border-b-2 border-brand-600" : "text-gray-500"}`}>
                  {t === "employee" ? "Users" : "Devices"}
                </button>
              ))}
            </div>
            <div className="p-4 shrink-0">
              <div className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search…" autoFocus
                  className="w-full pl-9 pr-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
              </div>
            </div>
            <div className="flex-1 overflow-y-auto px-4 pb-2 space-y-1 min-h-0" style={{ maxHeight: "300px" }}>
              {memberTab === "employee" ? (
                empList.length === 0
                  ? <p className="text-sm text-gray-500 text-center py-6">No users found.</p>
                  : empList.map(u => {
                    const isAdded = addedIds.has(u.id);
                    const otherGroups = (u.group_ids ?? []).filter(gid => gid !== createdGroupId).length;
                    return (
                      <button key={u.id}
                        onClick={() => !isAdded && addMember("employee", u.id)}
                        disabled={isAdded || addingId === u.id}
                        className={`flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg transition ${isAdded ? "opacity-60 cursor-default" : "hover:bg-gray-50"}`}>
                        <UserIcon size={14} className="text-gray-400 shrink-0" />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm text-gray-800 truncate">{u.name}</p>
                          {otherGroups > 0 && !isAdded && (
                            <p className="text-xs text-amber-500">In {otherGroups} other group{otherGroups !== 1 ? "s" : ""}</p>
                          )}
                        </div>
                        <span className="text-xs text-gray-400 truncate shrink-0 max-w-[120px]">{u.email}</span>
                        {isAdded
                          ? <span className="text-xs text-green-600 font-medium shrink-0 ml-1">Added</span>
                          : addingId === u.id
                            ? <span className="text-xs text-gray-400 shrink-0">Adding…</span>
                            : null}
                      </button>
                    );
                  })
              ) : (
                devList.length === 0
                  ? <p className="text-sm text-gray-500 text-center py-6">No devices found.</p>
                  : devList.map(d => {
                    const isAdded = addedIds.has(d.id);
                    return (
                      <button key={d.id}
                        onClick={() => !isAdded && addMember("device", d.id)}
                        disabled={isAdded || addingId === d.id}
                        className={`flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg transition ${isAdded ? "opacity-60 cursor-default" : "hover:bg-gray-50"}`}>
                        <Monitor size={14} className="text-gray-400 shrink-0" />
                        <span className="text-sm text-gray-800 flex-1 truncate">{d.hostname}</span>
                        <span className="text-xs text-gray-500 shrink-0">{d.employee_name ?? d.platform}</span>
                        {isAdded
                          ? <span className="text-xs text-green-600 font-medium shrink-0 ml-1">Added</span>
                          : addingId === d.id
                            ? <span className="text-xs text-gray-400 shrink-0">Adding…</span>
                            : null}
                      </button>
                    );
                  })
              )}
            </div>
            <div className="flex items-center justify-between px-5 py-4 border-t border-gray-100 shrink-0">
              <span className="text-xs text-gray-400">
                {addedIds.size > 0 ? `${addedIds.size} member${addedIds.size !== 1 ? "s" : ""} added` : "No members added yet"}
              </span>
              <button onClick={onClose}
                className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700">
                Done
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function GroupDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { loading, can } = useUser();
  const { data: group, mutate } = useSWR<GroupDetail>(["group", id], () => getGroup(id));
  const [adding, setAdding] = useState(false);
  const [addMemberGroupId, setAddMemberGroupId] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [showSubgroupModal, setShowSubgroupModal] = useState(false);
  const [busySubgroup, setBusySubgroup] = useState("");

  if (loading) return <PageLoader />;
  if (!can("groups", "view")) return <AccessDenied label="Groups" />;

  async function removeMember(memberId: string) {
    try { await removeGroupMember(id, memberId); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }

  async function handleArchiveSubgroup(subgroupId: string) {
    if (!confirm("Archive this subgroup? It will be hidden from the main list but you can unarchive it here.")) return;
    setBusySubgroup(subgroupId);
    try { await archiveGroup(subgroupId); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setBusySubgroup(""); }
  }

  async function handleUnarchiveSubgroup(subgroupId: string) {
    setBusySubgroup(subgroupId);
    try { await unarchiveGroup(subgroupId); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setBusySubgroup(""); }
  }

  async function handleDeleteSubgroup(subgroupId: string, subgroupName: string) {
    if (!confirm(`Delete "${subgroupName}"? This will also delete all its subgroups and cannot be undone.`)) return;
    setBusySubgroup(subgroupId);
    try { await deleteGroup(subgroupId); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setBusySubgroup(""); }
  }

  const activeChildren = (group?.children ?? []).filter(c => !c.is_archived);
  const archivedChildren = (group?.children ?? []).filter(c => c.is_archived);

  return (
    <div className="overflow-y-auto pr-1">
      <Link href="/groups" className="inline-flex items-center gap-1.5 text-gray-500 text-sm mb-3 hover:text-gray-700">
        <ArrowLeft size={13} /> All groups
      </Link>

      {group?.breadcrumb && group.breadcrumb.length > 0 && (
        <nav className="flex flex-wrap items-center gap-1 text-xs text-gray-500 mb-2">
          {group.breadcrumb.map((b, i) => (
            <span key={b.id} className="flex items-center gap-1">
              {i > 0 && <ChevronRight size={12} />}
              <Link href={`/groups/${b.id}`} className="hover:text-brand-600">{b.name}</Link>
            </span>
          ))}
          <ChevronRight size={12} />
          <span className="text-gray-800 font-medium">{group.name}</span>
        </nav>
      )}

      <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">{group?.name ?? "…"}</h1>
          <p className="text-gray-500 text-sm mt-0.5">
            {group?.member_count ?? 0} members
            {group?.child_count ? ` · ${group.child_count} subgroups` : ""}
            {group?.description ? ` · ${group.description}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {can("groups", "edit") && group && (
            <>
              <button onClick={() => setEditing(true)}
                className="flex items-center gap-1.5 px-3 py-2 text-sm border border-gray-200 rounded-lg hover:bg-gray-50 text-gray-700">
                <Pencil size={14} /> Edit
              </button>
              <button onClick={() => setAdding(true)}
                className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
                <UserPlus size={15} /> Add member
              </button>
            </>
          )}
        </div>
      </div>

      {can("groups", "create") && group && (
        <div className="bg-white border border-gray-200 rounded-xl p-4 mb-4 shadow-sm">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-gray-800 flex items-center gap-2">
              <FolderKanban size={15} className="text-gray-400" /> Subgroups
            </h2>
            <button
              onClick={() => setShowSubgroupModal(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-brand-600 text-white rounded-lg hover:bg-brand-700">
              <Plus size={12} /> Add subgroup
            </button>
          </div>

          {/* Active subgroups */}
          {activeChildren.length > 0 ? (
            <div className="grid gap-2 sm:grid-cols-2">
              {activeChildren.map(c => (
                <div key={c.id}
                  className="flex items-center gap-2 px-3 py-2.5 border border-gray-100 rounded-lg hover:border-brand-200 hover:bg-brand-50/30 transition">
                  <Link href={`/groups/${c.id}`} className="font-medium text-gray-800 text-sm flex-1 min-w-0 truncate">
                    {c.name}
                  </Link>
                  <span className="text-xs text-gray-400 shrink-0">{c.member_count}m · {c.child_count}s</span>
                  {can("groups", "edit") && (
                    <div className="flex items-center gap-0.5 shrink-0">
                      <button
                        onClick={e => { e.preventDefault(); setAddMemberGroupId(c.id); }}
                        disabled={busySubgroup === c.id}
                        title="Add member"
                        className="p-1.5 text-gray-400 hover:text-brand-600 rounded disabled:opacity-40">
                        <UserPlus size={12} />
                      </button>
                      <button
                        onClick={e => { e.preventDefault(); handleArchiveSubgroup(c.id); }}
                        disabled={busySubgroup === c.id}
                        title="Archive"
                        className="p-1.5 text-gray-400 hover:text-amber-600 rounded disabled:opacity-40">
                        <Archive size={12} />
                      </button>
                      <button
                        onClick={e => { e.preventDefault(); handleDeleteSubgroup(c.id, c.name); }}
                        disabled={busySubgroup === c.id}
                        title="Delete"
                        className="p-1.5 text-gray-400 hover:text-red-600 rounded disabled:opacity-40">
                        <Trash2 size={12} />
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-gray-400">No subgroups yet.</p>
          )}

          {/* Archived subgroups */}
          {archivedChildren.length > 0 && (
            <div className="mt-4 border-t border-dashed border-gray-200 pt-3">
              <p className="text-xs text-gray-400 font-medium mb-2 uppercase tracking-wide">Archived</p>
              <div className="grid gap-2 sm:grid-cols-2">
                {archivedChildren.map(c => (
                  <div key={c.id}
                    className="flex items-center gap-2 px-3 py-2.5 border border-dashed border-gray-200 rounded-lg bg-gray-50/50 opacity-70">
                    <span className="font-medium text-gray-500 text-sm flex-1 min-w-0 truncate">{c.name}</span>
                    <span className="text-xs text-gray-400 shrink-0">{c.member_count}m</span>
                    {can("groups", "edit") && (
                      <div className="flex items-center gap-0.5 shrink-0">
                        <button
                          onClick={() => handleUnarchiveSubgroup(c.id)}
                          disabled={busySubgroup === c.id}
                          title="Unarchive"
                          className="p-1.5 text-gray-400 hover:text-brand-600 rounded disabled:opacity-40">
                          <RotateCcw size={12} />
                        </button>
                        <button
                          onClick={() => handleDeleteSubgroup(c.id, c.name)}
                          disabled={busySubgroup === c.id}
                          title="Delete"
                          className="p-1.5 text-gray-400 hover:text-red-600 rounded disabled:opacity-40">
                          <Trash2 size={12} />
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-2.5 bg-gray-50 border-b border-gray-100 text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Direct members
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[600px]">
            <thead>
              <tr className="bg-gray-50/50 text-gray-500 text-xs">
                <th className="text-left px-4 py-2.5 font-medium">Member</th>
                <th className="text-left px-4 py-2.5 font-medium">Type</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {group?.members.map(m => (
                <tr key={m.id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      {m.member_type === "employee" ? <UserIcon size={14} className="text-gray-400" /> : <Monitor size={14} className="text-gray-400" />}
                      {m.member_type === "employee" && m.employee_id ? (
                        <Link href={`/users/${m.employee_id}`} className="font-medium text-gray-800 hover:text-brand-600">{m.label}</Link>
                      ) : m.member_type === "device" && m.device_id ? (
                        <Link href={`/devices/${m.device_id}`} className="font-medium text-gray-800 hover:text-brand-600">{m.label}</Link>
                      ) : (
                        <span className="font-medium text-gray-800">{m.label}</span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 capitalize text-gray-500">{m.member_type}</td>
                  <td className="px-4 py-3 text-right">
                    {can("groups", "edit") && (
                      <button onClick={() => removeMember(m.id)} className="text-gray-400 hover:text-red-600 p-1">
                        <Trash2 size={15} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {group?.members.length === 0 && (
                <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-500 text-sm">No members yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {(adding || addMemberGroupId) && (
        <AddMemberModal
          groupId={addMemberGroupId ?? id}
          onClose={() => { setAdding(false); setAddMemberGroupId(null); }}
          onSaved={() => mutate()}
        />
      )}
      {editing && group && (
        <EditGroupModal group={group} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); mutate(); }} />
      )}
      {showSubgroupModal && group && (
        <SubgroupCreateModal
          parentGroupId={id}
          parentGroupName={group.name}
          onClose={() => setShowSubgroupModal(false)}
          onSaved={() => mutate()}
        />
      )}
    </div>
  );
}
