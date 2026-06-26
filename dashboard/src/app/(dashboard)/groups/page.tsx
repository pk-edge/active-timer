"use client";
import PageLoader from "@/components/PageLoader";
import PageSkeleton from "@/components/PageSkeleton";
import { useState, useRef, useMemo } from "react";
import Link from "next/link";
import useSWR from "swr";
import {
  FolderKanban, Plus, Upload, X, Trash2, ChevronRight, ChevronDown,
  Search, FolderOpen, User as UserIcon, Monitor, Archive, RotateCcw,
} from "lucide-react";
import ConfirmDialog from "@/components/ConfirmDialog";
import GroupPicker from "@/components/GroupPicker";
import {
  getGroups, createGroup, deleteGroup, importGroupsCsv,
  getUsers, getDevices, addGroupMember, archiveGroup, unarchiveGroup,
} from "@/lib/api";
import { buildGroupTree, filterGroupTree, groupStats } from "@/lib/groupTree";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import type { Group, User, Device } from "@/types";
import type { GroupTreeNode } from "@/lib/groupTree";

function CreateModal({
  onClose, onSaved, defaultParentId,
}: {
  onClose: () => void; onSaved: () => void; defaultParentId?: string | null;
}) {
  const [tab, setTab] = useState<"new" | "csv">("new");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [parentId, setParentId] = useState(defaultParentId ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const [createdGroupId, setCreatedGroupId] = useState<string | null>(null);
  const [createdGroupName, setCreatedGroupName] = useState("");
  const [memberTab, setMemberTab] = useState<"employee" | "device">("employee");
  const [q, setQ] = useState("");
  const [addedIds, setAddedIds] = useState<Set<string>>(new Set());
  const [addingId, setAddingId] = useState("");

  const { data: users } = useSWR<User[]>("users-pick", () => getUsers());
  const { data: devices } = useSWR<Device[]>("devices-pick", () => getDevices());

  const step = createdGroupId ? "members" : "form";
  const groupLabel = parentId ? "Create subgroup" : "Create group";

  async function createNew() {
    setBusy(true); setErr("");
    try {
      const group = await createGroup({
        name,
        description: desc || undefined,
        parent_id: parentId || null,
      });
      setCreatedGroupId(group.id);
      setCreatedGroupName(group.name);
      onSaved();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }

  async function uploadCsv() {
    const f = fileRef.current?.files?.[0];
    if (!f) { setErr("Choose a CSV file"); return; }
    setBusy(true); setErr("");
    try {
      const r = await importGroupsCsv(f);
      setResult(`Created ${r.created_groups} groups, added ${r.added_members} members, skipped ${r.skipped?.length ?? 0}.`);
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

  const empList = useMemo(() => (users ?? []).filter(u =>
    !q || u.name.toLowerCase().includes(q.toLowerCase()) || u.email.toLowerCase().includes(q.toLowerCase())
  ), [users, q]);

  const devList = useMemo(() => (devices ?? []).filter(d =>
    !q || d.hostname.toLowerCase().includes(q.toLowerCase())
  ), [devices, q]);

  return (
    <div className="fixed inset-0 bg-black/30 backdrop-blur-[1px] flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md flex flex-col" style={{ maxHeight: "90vh" }}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 shrink-0">
          <div>
            <h2 className="font-semibold text-gray-900">
              {step === "form" ? groupLabel : "Add members"}
            </h2>
            {step === "members" && (
              <p className="text-xs text-gray-500 mt-0.5">
                &ldquo;{createdGroupName}&rdquo; created — add members or click Done to skip
              </p>
            )}
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700"><X size={18} /></button>
        </div>

        {step === "form" ? (
          <>
            <div className="flex border-b border-gray-100 shrink-0">
              {(["new", "csv"] as const).map(t => (
                <button key={t} onClick={() => setTab(t)}
                  className={`flex-1 py-2.5 text-sm font-medium transition ${tab === t ? "text-brand-600 border-b-2 border-brand-600" : "text-gray-500"}`}>
                  {t === "new" ? "New group" : "Upload CSV"}
                </button>
              ))}
            </div>
            <div className="p-5 space-y-3 overflow-y-auto">
              {tab === "new" ? (
                <>
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Parent group (optional)</label>
                    <GroupPicker value={parentId} onChange={setParentId} allowEmpty emptyLabel="Top-level group" className="w-full" />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Name</label>
                    <input value={name} onChange={e => setName(e.target.value)} autoFocus
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Description (optional)</label>
                    <input value={desc} onChange={e => setDesc(e.target.value)}
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
                  </div>
                </>
              ) : (
                <>
                  <p className="text-xs text-gray-600 leading-relaxed">
                    CSV columns: <code className="bg-gray-100 px-1 rounded">group</code>,{" "}
                    <code className="bg-gray-100 px-1 rounded">identifier</code>,{" "}
                    <code className="bg-gray-100 px-1 rounded">type</code>,{" "}
                    <code className="bg-gray-100 px-1 rounded">parent</code> (optional).
                  </p>
                  <input ref={fileRef} type="file" accept=".csv"
                    className="block w-full text-sm text-gray-600 file:mr-3 file:py-2 file:px-3 file:rounded-lg file:border-0 file:bg-brand-50 file:text-brand-600" />
                  {result && <p className="text-green-600 text-sm">{result}</p>}
                </>
              )}
              {err && <p className="text-red-500 text-sm">{err}</p>}
            </div>
            <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100 shrink-0">
              <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Close</button>
              {tab === "new" ? (
                <button onClick={createNew} disabled={busy || !name.trim()}
                  className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
                  {busy ? "Creating…" : "Create"}
                </button>
              ) : (
                <button onClick={uploadCsv} disabled={busy}
                  className="flex items-center gap-1.5 px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
                  <Upload size={14} /> {busy ? "Uploading…" : "Import"}
                </button>
              )}
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
            <div className="flex-1 overflow-y-auto px-4 pb-2 space-y-1 min-h-0" style={{ maxHeight: "320px" }}>
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
                            <p className="text-xs text-amber-500">Already in {otherGroups} other group{otherGroups !== 1 ? "s" : ""}</p>
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

function TreeRow({
  node, depth, expanded, onToggle,
  canCreate, canEdit, canDelete,
  onDelete, onArchive, onUnarchive, onCreateChild,
}: {
  node: GroupTreeNode; depth: number; expanded: Set<string>;
  onToggle: (id: string) => void;
  canCreate: boolean; canEdit: boolean; canDelete: boolean;
  onDelete: (g: Group) => void;
  onArchive: (g: Group) => void;
  onUnarchive: (g: Group) => void;
  onCreateChild: (parentId: string) => void;
}) {
  const g = node.group;
  const hasChildren = node.children.length > 0;
  const isOpen = expanded.has(g.id);
  const isArchived = g.is_archived;

  return (
    <>
      <div
        className={`flex items-center gap-2 px-3 py-2.5 border-b border-gray-50 group/row transition ${isArchived ? "bg-gray-50/60 opacity-60 hover:opacity-80" : "hover:bg-gray-50"}`}
        style={{ paddingLeft: `${depth * 20 + 12}px` }}
      >
        <button
          type="button"
          onClick={() => hasChildren && onToggle(g.id)}
          className={`w-5 h-5 flex items-center justify-center shrink-0 rounded ${hasChildren ? "text-gray-500 hover:bg-gray-100" : "invisible"}`}
        >
          {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        {hasChildren && isOpen ? (
          <FolderOpen size={16} className="text-brand-500 shrink-0" />
        ) : (
          <FolderKanban size={16} className={`shrink-0 ${isArchived ? "text-amber-400" : "text-gray-400"}`} />
        )}
        <Link href={`/groups/${g.id}`} className="flex-1 min-w-0">
          <p className={`font-medium truncate group-hover/row:text-brand-600 ${isArchived ? "text-gray-500" : "text-gray-800"}`}>{g.name}</p>
          {g.description && <p className="text-xs text-gray-500 truncate">{g.description}</p>}
        </Link>
        <div className="flex items-center gap-2 shrink-0 text-xs text-gray-500">
          {isArchived && (
            <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-amber-50 text-amber-600 border border-amber-200">
              Archived
            </span>
          )}
          {!isArchived && g.child_count > 0 && <span>{g.child_count} sub</span>}
          {!isArchived && <span>{g.member_count} members</span>}
          {canCreate && !isArchived && (
            <button
              onClick={() => onCreateChild(g.id)}
              title="Add subgroup"
              className="opacity-0 group-hover/row:opacity-100 p-1 text-gray-400 hover:text-brand-600 transition"
            >
              <Plus size={14} />
            </button>
          )}
          {canEdit && !isArchived && (
            <button
              onClick={() => onArchive(g)}
              title="Archive"
              className="opacity-0 group-hover/row:opacity-100 p-1 text-gray-400 hover:text-amber-600 transition"
            >
              <Archive size={14} />
            </button>
          )}
          {canEdit && isArchived && (
            <button
              onClick={() => onUnarchive(g)}
              title="Unarchive"
              className="p-1 text-gray-400 hover:text-brand-600 transition"
            >
              <RotateCcw size={14} />
            </button>
          )}
          {canDelete && (
            <button onClick={() => onDelete(g)} className="p-1 text-gray-400 hover:text-red-600">
              <Trash2 size={14} />
            </button>
          )}
          <Link href={`/groups/${g.id}`} className="p-1 text-gray-400 hover:text-brand-600">
            <ChevronRight size={16} />
          </Link>
        </div>
      </div>
      {hasChildren && isOpen && node.children.map(child => (
        <TreeRow
          key={child.group.id}
          node={child}
          depth={depth + 1}
          expanded={expanded}
          onToggle={onToggle}
          canCreate={canCreate}
          canEdit={canEdit}
          canDelete={canDelete}
          onDelete={onDelete}
          onArchive={onArchive}
          onUnarchive={onUnarchive}
          onCreateChild={onCreateChild}
        />
      ))}
    </>
  );
}

export default function GroupsPage() {
  const { loading, can } = useUser();
  const [showArchived, setShowArchived] = useState(false);
  const { data: groups, mutate, isLoading } = useSWR<Group[]>(
    ["groups", showArchived],
    () => getGroups(showArchived),
  );
  const [creating, setCreating] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Group | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [pendingArchive, setPendingArchive] = useState<Group | null>(null);
  const [archiving, setArchiving] = useState(false);
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const stats = useMemo(() => groupStats((groups ?? []).filter(g => !g.is_archived)), [groups]);
  const tree = useMemo(() => {
    const t = buildGroupTree(groups ?? []);
    return search ? filterGroupTree(t, search) : t;
  }, [groups, search]);

  const toggle = (id: string) => {
    setExpanded(prev => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  };

  if (loading) return <PageLoader />;
  if (!can("groups", "view")) return <AccessDenied label="Groups" />;
  if (isLoading && !groups) return <PageSkeleton variant="table" />;

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try { await deleteGroup(pendingDelete.id); mutate(); setPendingDelete(null); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setDeleting(false); }
  }

  async function confirmArchive() {
    if (!pendingArchive) return;
    setArchiving(true);
    try { await archiveGroup(pendingArchive.id); mutate(); setPendingArchive(null); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setArchiving(false); }
  }

  async function handleUnarchive(g: Group) {
    try { await unarchiveGroup(g.id); mutate(); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Groups</h1>
          <p className="text-gray-500 text-sm mt-0.5">Organize teams and devices in nested groups</p>
        </div>
        {can("groups", "create") && (
          <button onClick={() => setCreating("")}
            className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
            <Plus size={15} /> New group
          </button>
        )}
      </div>

      <div className="flex flex-wrap gap-2 mb-4">
        {[
          { label: "Total", value: stats.total, color: "bg-brand-50 text-brand-700" },
          { label: "Top-level", value: stats.roots, color: "bg-gray-50 text-gray-600" },
          { label: "With subgroups", value: stats.withChildren, color: "bg-purple-50 text-purple-600" },
          { label: "Members", value: stats.members, color: "bg-blue-50 text-blue-600" },
        ].map(s => (
          <span key={s.label} className={`text-xs font-medium px-2.5 py-1 rounded-full ${s.color}`}>
            {s.label}: {s.value}
          </span>
        ))}
      </div>

      <div className="flex items-center gap-2 mb-3">
        <div className="relative flex-1 max-w-sm">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search groups…"
            className="w-full pl-9 pr-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300 bg-white"
          />
        </div>
        {can("groups", "edit") && (
          <button
            onClick={() => setShowArchived(v => !v)}
            className={`flex items-center gap-1.5 px-3 py-2 text-sm rounded-lg border transition ${showArchived ? "bg-amber-50 border-amber-200 text-amber-700" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}
          >
            <Archive size={13} />
            {showArchived ? "Hide archived" : "Show archived"}
          </button>
        )}
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-2.5 bg-gray-50 border-b border-gray-100 text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Group hierarchy
        </div>
        {tree.length === 0 ? (
          <p className="px-4 py-12 text-center text-gray-500 text-sm">
            {search ? "No groups match your search." : "No groups yet. Create one to get started."}
          </p>
        ) : (
          tree.map(node => (
            <TreeRow
              key={node.group.id}
              node={node}
              depth={0}
              expanded={expanded}
              onToggle={toggle}
              canCreate={can("groups", "create")}
              canEdit={can("groups", "edit")}
              canDelete={can("groups", "delete")}
              onDelete={setPendingDelete}
              onArchive={setPendingArchive}
              onUnarchive={handleUnarchive}
              onCreateChild={id => setCreating(id)}
            />
          ))
        )}
      </div>

      {creating !== null && (
        <CreateModal
          defaultParentId={creating || null}
          onClose={() => setCreating(null)}
          onSaved={() => { mutate(); if (creating) setExpanded(p => new Set(p).add(creating)); }}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        title={`Delete "${pendingDelete?.name}"?`}
        message={
          pendingDelete && pendingDelete.child_count > 0
            ? "This will also remove all subgroups. Members won't be deleted but will lose group access."
            : "This group will be removed. Members won't be deleted but will lose group access."
        }
        confirmLabel="Delete Group"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />

      <ConfirmDialog
        open={!!pendingArchive}
        title={`Archive "${pendingArchive?.name}"?`}
        message="The group will be hidden from the main list. You can restore it any time with 'Show archived'."
        confirmLabel="Archive Group"
        loading={archiving}
        onConfirm={confirmArchive}
        onCancel={() => setPendingArchive(null)}
      />
    </div>
  );
}
