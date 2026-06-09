"use client";
import { useState, useRef } from "react";
import Link from "next/link";
import useSWR from "swr";
import { FolderKanban, Plus, Upload, X, Trash2, ChevronRight } from "lucide-react";
import ConfirmDialog from "@/components/ConfirmDialog";
import { getGroups, createGroup, deleteGroup, importGroupsCsv } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { Group } from "@/types";

function CreateModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [tab, setTab] = useState<"new" | "csv">("new");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<string>("");
  const fileRef = useRef<HTMLInputElement>(null);

  async function createNew() {
    setBusy(true); setErr("");
    try { await createGroup({ name, description: desc }); onSaved(); }
    catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }
  async function uploadCsv() {
    const f = fileRef.current?.files?.[0];
    if (!f) { setErr("Choose a CSV file"); return; }
    setBusy(true); setErr("");
    try {
      const r = await importGroupsCsv(f);
      setResult(`Created ${r.created_groups} groups, added ${r.added_members} members, skipped ${r.skipped?.length ?? 0}.`);
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">Create groups</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="flex border-b border-gray-100">
          {(["new", "csv"] as const).map(t => (
            <button key={t} onClick={() => setTab(t)}
                    className={`flex-1 py-2.5 text-sm font-medium transition ${tab === t ? "text-brand-600 border-b-2 border-brand-600" : "text-gray-500"}`}>
              {t === "new" ? "New group" : "Upload CSV"}
            </button>
          ))}
        </div>
        <div className="p-5 space-y-3">
          {tab === "new" ? (
            <>
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">Group name</label>
                <input value={name} onChange={e => setName(e.target.value)} autoFocus
                       className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">Description (optional)</label>
                <input value={desc} onChange={e => setDesc(e.target.value)}
                       className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
              </div>
            </>
          ) : (
            <>
              <p className="text-xs text-gray-500">CSV columns: <code className="bg-gray-100 px-1 rounded">group,identifier,type</code> — type is <code className="bg-gray-100 px-1 rounded">employee</code> (email) or <code className="bg-gray-100 px-1 rounded">device</code> (hostname).</p>
              <input ref={fileRef} type="file" accept=".csv" className="block w-full text-sm text-gray-600 file:mr-3 file:py-2 file:px-3 file:rounded-lg file:border-0 file:bg-brand-50 file:text-brand-600" />
              {result && <p className="text-green-600 text-sm">{result}</p>}
            </>
          )}
          {err && <p className="text-red-500 text-sm">{err}</p>}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Close</button>
          {tab === "new" ? (
            <button onClick={createNew} disabled={busy || !name} className="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
              {busy ? "Creating…" : "Create"}
            </button>
          ) : (
            <button onClick={uploadCsv} disabled={busy} className="flex items-center gap-1.5 px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50">
              <Upload size={14} /> {busy ? "Uploading…" : "Import"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default function GroupsPage() {
  const { loading, can } = useUser();
  const { data: groups, mutate } = useSWR<Group[]>("groups", getGroups);
  const [creating,       setCreating]      = useState(false);
  const [pendingDelete,  setPendingDelete]  = useState<Group | null>(null);
  const [deleting,       setDeleting]       = useState(false);

  if (loading) return null;
  if (!can("groups", "view")) return <AccessDenied label="Groups" />;

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try { await deleteGroup(pendingDelete.id); mutate(); setPendingDelete(null); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
    finally { setDeleting(false); }
  }

  return (
    <div className="overflow-y-auto pr-1">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Groups</h1>
          <p className="text-gray-500 text-sm mt-0.5">{groups?.length ?? 0} groups</p>
        </div>
        {can("groups", "create") && (
          <button onClick={() => setCreating(true)} className="flex items-center gap-2 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700">
            <Plus size={15} /> Create group
          </button>
        )}
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[600px]">
          <thead>
            <tr className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
              <th className="text-left px-4 py-3 font-semibold">Group</th>
              <th className="text-left px-4 py-3 font-semibold">Members</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {groups?.map(g => (
              <tr key={g.id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-4 py-3">
                  <Link href={`/groups/${g.id}`} className="flex items-center gap-2 group">
                    <FolderKanban size={15} className="text-gray-400" />
                    <span className="font-medium text-gray-800 group-hover:text-brand-600">{g.name}</span>
                  </Link>
                  {g.description && <p className="text-xs text-gray-400 mt-0.5 ml-6">{g.description}</p>}
                </td>
                <td className="px-4 py-3 text-gray-500">{g.member_count}</td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  {can("groups", "delete") && <button onClick={() => setPendingDelete(g)} className="text-gray-400 hover:text-red-600 p-1"><Trash2 size={15} /></button>}
                  <Link href={`/groups/${g.id}`} className="text-gray-400 hover:text-brand-600 p-1 inline-flex"><ChevronRight size={16} /></Link>
                </td>
              </tr>
            ))}
            {groups?.length === 0 && <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-400 text-sm">No groups yet. Create one to get started.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>

      {creating && <CreateModal onClose={() => setCreating(false)} onSaved={() => { setCreating(false); mutate(); }} />}

      <ConfirmDialog
        open={!!pendingDelete}
        title={`Delete "${pendingDelete?.name}"?`}
        message="This group will be removed. Members won't be deleted but will lose group access."
        confirmLabel="Delete Group"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
