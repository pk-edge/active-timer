"use client";
import { useState, useRef } from "react";
import useSWR from "swr";
import { format, parseISO } from "date-fns";
import { DownloadCloud, Upload, CheckCircle2, Download, Apple, Monitor, History, User, Trash2, Link2, Copy, Check } from "lucide-react";
import { getInstallers, uploadInstaller, activateInstaller, deleteInstaller, getInstallerDownload, getInstallerDownloads, getCompanyInfo, fmtBytes, BASE } from "@/lib/api";
import type { CompanyInfo } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { Installer, InstallerDownload } from "@/types";

function UploadForm({ onUploaded }: { onUploaded: () => void }) {
  const [platform, setPlatform] = useState("macos");
  const [itype, setItype] = useState("dmg");
  const [version, setVersion] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const types = platform === "macos" ? ["dmg"] : ["msi", "exe"];

  async function submit() {
    const f = fileRef.current?.files?.[0];
    if (!f || !version) { setErr("Version and file are required"); return; }
    setBusy(true); setErr("");
    try {
      await uploadInstaller({ platform, installer_type: types.includes(itype) ? itype : types[0], version, notes, file: f });
      setVersion(""); setNotes(""); if (fileRef.current) fileRef.current.value = "";
      onUploaded();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
      <p className="text-sm font-semibold text-gray-800 mb-3">Upload new agent build</p>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Platform</label>
          <select value={platform} onChange={e => { setPlatform(e.target.value); setItype(e.target.value === "macos" ? "dmg" : "msi"); }}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
            <option value="macos">macOS</option><option value="windows">Windows</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Type</label>
          <select value={itype} onChange={e => setItype(e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
            {types.map(t => <option key={t} value={t}>{t.toUpperCase()}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Version</label>
          <input value={version} onChange={e => setVersion(e.target.value)} placeholder="1.1.0"
                 className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Notes</label>
          <input value={notes} onChange={e => setNotes(e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
      </div>
      <div className="flex items-center gap-3 mt-3">
        <input ref={fileRef} type="file" className="block text-sm text-gray-600 file:mr-3 file:py-2 file:px-3 file:rounded-lg file:border-0 file:bg-brand-50 file:text-brand-600" />
        <button onClick={submit} disabled={busy} className="flex items-center gap-1.5 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700 disabled:opacity-50 ml-auto">
          <Upload size={14} /> {busy ? "Uploading…" : "Upload"}
        </button>
      </div>
      {err && <p className="text-red-500 text-sm mt-2">{err}</p>}
    </div>
  );
}

const PLATFORM_ICON: Record<string, React.ReactNode> = {
  macos:   <Apple   size={13} className="shrink-0" />,
  windows: <Monitor size={13} className="shrink-0" />,
};

function CopyableLink({ label, icon, url }: { label: string; icon: React.ReactNode; url: string }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(url).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); });
  }
  return (
    <div className="flex items-center gap-3 p-3 bg-gray-50 border border-gray-200 rounded-lg">
      <div className="flex items-center gap-2 text-gray-600 shrink-0">
        {icon}
        <span className="text-sm font-medium">{label}</span>
      </div>
      <code className="flex-1 text-xs text-gray-500 truncate min-w-0 bg-white border border-gray-200 rounded px-2 py-1.5 font-mono">
        {url}
      </code>
      <button
        onClick={copy}
        className={`flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg border transition shrink-0 ${
          copied
            ? "bg-green-50 border-green-200 text-green-600"
            : "bg-white border-gray-200 text-gray-600 hover:border-brand-300 hover:text-brand-600"
        }`}
      >
        {copied ? <><Check size={12} /> Copied</> : <><Copy size={12} /> Copy</>}
      </button>
    </div>
  );
}

export default function AgentsPage() {
  const { loading, can } = useUser();
  const { data: installers, mutate } = useSWR<Installer[]>("installers", getInstallers);
  const { data: downloads }          = useSWR<InstallerDownload[]>("installer-downloads", getInstallerDownloads);
  const { data: companyInfo }        = useSWR<CompanyInfo>("company-info", getCompanyInfo);

  if (loading) return null;
  if (!can("agents", "view")) return <AccessDenied label="Agents" />;

  async function activate(id: string) {
    try { await activateInstaller(id); mutate(); } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }
  async function remove(id: string, version: string) {
    if (!confirm(`Delete v${version}? This cannot be undone.`)) return;
    try { await deleteInstaller(id); mutate(); } catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }
  async function download(id: string) {
    try { const { download_url } = await getInstallerDownload(id); window.open(download_url, "_blank"); }
    catch (e) { alert(String(e instanceof Error ? e.message : e)); }
  }

  const active = { macos: installers?.find(i => i.platform === "macos" && i.is_active),
                   windows: installers?.find(i => i.platform === "windows" && i.is_active) };

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1 space-y-6">
      <div>
        <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">Agents</h1>
        <p className="text-gray-500 text-sm mt-0.5">Distribute and update the desktop agent. The active version is what devices auto-update to.</p>
      </div>

      {/* Active per OS */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {(["macos", "windows"] as const).map(p => {
          const a = active[p];
          return (
            <div key={p} className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm flex items-center gap-3">
              {p === "macos" ? <Apple size={22} className="text-gray-700" /> : <Monitor size={22} className="text-gray-700" />}
              <div className="flex-1">
                <p className="text-sm font-medium text-gray-800">{p === "macos" ? "macOS" : "Windows"}</p>
                <p className="text-xs text-gray-400">{a ? `Active: v${a.version} (${a.installer_type.toUpperCase()})` : "No active build"}</p>
              </div>
              {a && can("agents", "view") && (
                <button onClick={() => download(a.id)} className="flex items-center gap-1.5 text-sm text-brand-600 hover:text-brand-700">
                  <Download size={14} /> Download
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* Public download links */}
      {companyInfo && (
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-1">
            <Link2 size={15} className="text-gray-400" />
            <p className="text-sm font-semibold text-gray-800">Public Download Links</p>
          </div>
          <p className="text-xs text-gray-400 mb-4">
            Share these links with employees. Each visit generates a fresh 15-minute signed download — no credentials exposed.
          </p>
          <div className="space-y-2.5">
            <CopyableLink
              label="macOS"
              icon={<Apple size={14} />}
              url={`${BASE}/v1/downloads/${companyInfo.enrollment_token}/TimeTracker.dmg`}
            />
            <CopyableLink
              label="Windows"
              icon={<Monitor size={14} />}
              url={`${BASE}/v1/downloads/${companyInfo.enrollment_token}/TimeTracker.msi`}
            />
          </div>
        </div>
      )}

      {can("agents", "create") && <UploadForm onUploaded={mutate} />}

      {/* Version history */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-3 border-b border-gray-100 bg-gray-50 flex items-center gap-2">
          <DownloadCloud size={15} className="text-gray-400" />
          <p className="text-sm font-semibold text-gray-700">Version history</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[720px]">
          <thead>
            <tr className="text-gray-500 text-xs uppercase tracking-wide border-b border-gray-100">
              <th className="text-left px-4 py-2.5 font-semibold">Platform</th>
              <th className="text-left px-4 py-2.5 font-semibold">Version</th>
              <th className="text-left px-4 py-2.5 font-semibold">Type</th>
              <th className="text-left px-4 py-2.5 font-semibold">Size</th>
              <th className="text-left px-4 py-2.5 font-semibold">Status</th>
              <th className="px-4 py-2.5" />
            </tr>
          </thead>
          <tbody>
            {installers?.map(i => (
              <tr key={i.id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-4 py-3 capitalize text-gray-700">{i.platform}</td>
                <td className="px-4 py-3 font-medium text-gray-800">v{i.version}</td>
                <td className="px-4 py-3 text-gray-500 uppercase">{i.installer_type}</td>
                <td className="px-4 py-3 text-gray-500">{fmtBytes(i.file_size)}</td>
                <td className="px-4 py-3">
                  {i.is_active
                    ? <span className="inline-flex items-center gap-1 text-xs text-green-600"><CheckCircle2 size={13} /> Active</span>
                    : <span className="text-xs text-gray-400">inactive</span>}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  <button onClick={() => download(i.id)} className="text-gray-400 hover:text-brand-600 p-1"><Download size={15} /></button>
                  {can("agents", "edit") && !i.is_active && (
                    <button onClick={() => activate(i.id)} className="text-xs text-brand-600 hover:text-brand-700 ml-2">Activate</button>
                  )}
                  {can("agents", "delete") && (
                    <button onClick={() => remove(i.id, i.version)} title="Delete" className="text-gray-300 hover:text-red-500 p-1 ml-1 inline-flex">
                      <Trash2 size={14} />
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {installers?.length === 0 && <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400 text-sm">No agent builds uploaded yet.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>

      {/* Download history */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-3 border-b border-gray-100 bg-gray-50 flex items-center gap-2">
          <History size={15} className="text-gray-400" />
          <p className="text-sm font-semibold text-gray-700">Download history</p>
          {downloads && downloads.length > 0 && (
            <span className="ml-auto text-xs text-gray-400">{downloads.length} record{downloads.length !== 1 ? "s" : ""}</span>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[640px]">
            <thead>
              <tr className="text-gray-500 text-xs uppercase tracking-wide border-b border-gray-100">
                <th className="text-left px-4 py-2.5 font-semibold">User</th>
                <th className="text-left px-4 py-2.5 font-semibold">Platform</th>
                <th className="text-left px-4 py-2.5 font-semibold">Version</th>
                <th className="text-left px-4 py-2.5 font-semibold">Downloaded at</th>
              </tr>
            </thead>
            <tbody>
              {downloads?.map(dl => (
                <tr key={dl.id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="inline-flex items-center justify-center w-6 h-6 rounded-full bg-brand-100 text-brand-600 shrink-0">
                        <User size={12} />
                      </span>
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-gray-800 truncate">{dl.downloaded_by_name}</p>
                        <p className="text-xs text-gray-400 truncate">{dl.downloaded_by_email}</p>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-gray-600 capitalize">
                      {PLATFORM_ICON[dl.platform]}
                      {dl.platform}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-sm font-medium text-gray-800">v{dl.version}</span>
                    <span className="ml-1.5 text-xs text-gray-400 uppercase">{dl.installer_type}</span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">
                    {format(parseISO(dl.downloaded_at), "MMM d, yyyy · h:mm a")}
                  </td>
                </tr>
              ))}
              {downloads?.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-4 py-10 text-center text-gray-400 text-sm">
                    No downloads recorded yet.
                  </td>
                </tr>
              )}
              {!downloads && (
                <tr>
                  <td colSpan={4} className="px-4 py-6 text-center text-gray-300 text-sm animate-pulse">
                    Loading…
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
