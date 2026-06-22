"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useRef } from "react";
import useSWR from "swr";
import { format, parseISO } from "date-fns";
import { DownloadCloud, Upload, CheckCircle2, Download, Apple, Monitor, History, User, Trash2, Link2, Copy, Check, SlidersHorizontal, Activity, AlertTriangle, Rocket } from "lucide-react";
import { getInstallers, uploadInstaller, activateInstaller, deleteInstaller, getInstallerDownload, getInstallerDownloads, getCompanyInfo, patchInstaller, getUpdateRuns, fmtBytes, BASE } from "@/lib/api";
import type { CompanyInfo } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { Installer, InstallerDownload, UpdateRun, UpdateRunsResponse } from "@/types";

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
          <label className="block text-xs text-gray-600 mb-1">Platform</label>
          <select value={platform} onChange={e => { setPlatform(e.target.value); setItype(e.target.value === "macos" ? "dmg" : "msi"); }}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
            <option value="macos">macOS</option><option value="windows">Windows</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-600 mb-1">Type</label>
          <select value={itype} onChange={e => setItype(e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
            {types.map(t => <option key={t} value={t}>{t.toUpperCase()}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-600 mb-1">Version</label>
          <input value={version} onChange={e => setVersion(e.target.value)} placeholder="1.1.0"
                 className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <div>
          <label className="block text-xs text-gray-600 mb-1">Notes</label>
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
      <code className="flex-1 text-xs text-gray-600 truncate min-w-0 bg-white border border-gray-200 rounded px-2 py-1.5 font-mono">
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

function DistributionEditor({ inst, onSaved }: { inst: Installer; onSaved: () => void }) {
  const [rollout, setRollout]   = useState(inst.rollout_percent ?? 100);
  const [mandatory, setMandatory] = useState(inst.mandatory ?? false);
  const [channel, setChannel]   = useState(inst.channel ?? "stable");
  const [minVer, setMinVer]     = useState(inst.min_supported_version ?? "");
  const [expires, setExpires]   = useState(inst.expires_at ? inst.expires_at.slice(0, 16) : "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg]   = useState("");

  async function save() {
    setBusy(true); setMsg("");
    try {
      await patchInstaller(inst.id, {
        rollout_percent: rollout, mandatory, channel,
        min_supported_version: minVer.trim() || null,
        expires_at: expires ? new Date(expires).toISOString() : null,
      });
      setMsg("Saved"); onSaved(); setTimeout(() => setMsg(""), 2000);
    } catch (e) { setMsg(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-3">
        {inst.platform === "macos" ? <Apple size={15} className="text-gray-600" /> : <Monitor size={15} className="text-gray-600" />}
        <p className="text-sm font-semibold text-gray-800">{inst.platform === "macos" ? "macOS" : "Windows"} · v{inst.version}</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="sm:col-span-2">
          <label className="block text-xs text-gray-600 mb-1">Rollout — {rollout}% of devices</label>
          <div className="flex items-center gap-3">
            <input type="range" min={0} max={100} value={rollout} onChange={e => setRollout(Number(e.target.value))} className="flex-1 accent-brand-600" />
            <input type="number" min={0} max={100} value={rollout} onChange={e => setRollout(Math.max(0, Math.min(100, Number(e.target.value))))} className="w-16 border border-gray-300 rounded-lg px-2 py-1 text-sm" />
          </div>
        </div>
        <div>
          <label className="block text-xs text-gray-600 mb-1">Channel</label>
          <select value={channel} onChange={e => setChannel(e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
            <option value="stable">stable</option><option value="beta">beta</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-600 mb-1">Min supported version</label>
          <input value={minVer} onChange={e => setMinVer(e.target.value)} placeholder="e.g. 1.0.0" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <div className="sm:col-span-2">
          <label className="block text-xs text-gray-600 mb-1">Manifest expiry (optional) — agents reject the update after this time</label>
          <input type="datetime-local" value={expires} onChange={e => setExpires(e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-700 sm:col-span-2">
          <input type="checkbox" checked={mandatory} onChange={e => setMandatory(e.target.checked)} className="accent-brand-600" />
          Mandatory — overrides rollout %, forces every device to update
        </label>
      </div>
      <div className="flex items-center gap-3 mt-3">
        {msg && <span className={`text-xs ${msg === "Saved" ? "text-green-600" : "text-red-500"}`}>{msg}</span>}
        <button onClick={save} disabled={busy} className="ml-auto flex items-center gap-1.5 bg-brand-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-brand-700 disabled:opacity-50">
          <SlidersHorizontal size={14} /> {busy ? "Saving…" : "Save distribution"}
        </button>
      </div>
    </div>
  );
}

const _STATUS_STYLE = (s: string): string => {
  if (s.endsWith("SUCCESS")) return "bg-green-50 text-green-600 border-green-200";
  if (s.includes("FAILED") || s.startsWith("ROLLBACK")) return "bg-red-50 text-red-600 border-red-200";
  return "bg-gray-50 text-gray-600 border-gray-200";
};

function FleetStatus() {
  const { data } = useSWR<UpdateRunsResponse>("update-runs", getUpdateRuns);
  const adoption = data ? Object.entries(data.adoption).sort((a, b) => b[1] - a[1]) : [];
  const total = adoption.reduce((n, [, c]) => n + c, 0);

  return (
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
      <div className="px-4 py-3 border-b border-gray-100 bg-gray-50 flex items-center gap-2">
        <Activity size={15} className="text-gray-600" />
        <p className="text-sm font-semibold text-gray-700">Fleet version adoption &amp; update status</p>
      </div>
      <div className="p-4 space-y-4">
        {/* Adoption bars */}
        <div className="space-y-2">
          {adoption.length === 0 && <p className="text-sm text-gray-600">No devices reporting yet.</p>}
          {adoption.map(([ver, count]) => (
            <div key={ver} className="flex items-center gap-3">
              <span className="text-xs font-medium text-gray-700 w-24 shrink-0 truncate">{ver === "unknown" ? "unknown" : `v${ver}`}</span>
              <div className="flex-1 bg-gray-100 rounded-full h-3 overflow-hidden">
                <div className="bg-brand-500 h-3" style={{ width: total ? `${(count / total) * 100}%` : "0%" }} />
              </div>
              <span className="text-xs text-gray-600 w-12 text-right shrink-0">{count}</span>
            </div>
          ))}
        </div>

        {/* Recent update runs */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[640px]">
            <thead>
              <tr className="text-gray-600 text-xs uppercase tracking-wide border-b border-gray-100">
                <th className="text-left px-3 py-2 font-semibold">Device</th>
                <th className="text-left px-3 py-2 font-semibold">Update</th>
                <th className="text-left px-3 py-2 font-semibold">Status</th>
                <th className="text-left px-3 py-2 font-semibold">Progress</th>
                <th className="text-left px-3 py-2 font-semibold">When</th>
              </tr>
            </thead>
            <tbody>
              {data?.runs.slice(0, 100).map((r: UpdateRun) => (
                <tr key={r.id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-3 py-2 font-mono text-xs text-gray-600">{r.device_id.slice(0, 8)}</td>
                  <td className="px-3 py-2 text-gray-700">{r.from_version ? `v${r.from_version} → ` : ""}v{r.target_version}</td>
                  <td className="px-3 py-2">
                    <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border ${_STATUS_STYLE(r.status)}`}>
                      {(r.status.includes("FAILED") || r.status.startsWith("ROLLBACK")) && <AlertTriangle size={11} />}
                      {r.status}
                    </span>
                    {r.error_code && <span className="block text-[11px] text-red-400 mt-0.5 truncate max-w-[200px]">{r.error_code}</span>}
                  </td>
                  <td className="px-3 py-2 text-gray-600">{r.progress}%</td>
                  <td className="px-3 py-2 text-xs text-gray-600 whitespace-nowrap">{r.updated_at ? format(parseISO(r.updated_at), "MMM d · h:mm a") : "—"}</td>
                </tr>
              ))}
              {data && data.runs.length === 0 && (
                <tr><td colSpan={5} className="px-3 py-8 text-center text-gray-600 text-sm">No update activity reported yet.</td></tr>
              )}
              {!data && <tr><td colSpan={5} className="px-3 py-6 text-center text-gray-500 text-sm animate-pulse">Loading…</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const { loading, can } = useUser();
  const { data: installers, mutate } = useSWR<Installer[]>("installers", getInstallers);
  const { data: downloads }          = useSWR<InstallerDownload[]>("installer-downloads", getInstallerDownloads);
  const { data: companyInfo }        = useSWR<CompanyInfo>("company-info", getCompanyInfo);

  if (loading) return <PageLoader />;
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
        <p className="text-gray-600 text-sm mt-0.5">Distribute and update the desktop agent. The active version is what devices auto-update to.</p>
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
                <p className="text-xs text-gray-600">{a ? `Active: v${a.version} (${a.installer_type.toUpperCase()})` : "No active build"}</p>
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
            <Link2 size={15} className="text-gray-600" />
            <p className="text-sm font-semibold text-gray-800">Public Download Links</p>
          </div>
          <p className="text-xs text-gray-600 mb-4">
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

      {/* Distribution / rollout controls */}
      {can("agents", "edit") && (active.macos || active.windows) && (
        <div>
          <div className="flex items-center gap-2 mb-2">
            <Rocket size={15} className="text-gray-600" />
            <p className="text-sm font-semibold text-gray-800">Distribution &amp; rollout</p>
          </div>
          <p className="text-xs text-gray-600 mb-3">
            Control how the active build reaches this company&apos;s devices — staged rollout %, mandatory updates,
            release channel, and minimum supported version. Changes apply on the next device update check.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {active.macos   && <DistributionEditor inst={active.macos}   onSaved={mutate} />}
            {active.windows && <DistributionEditor inst={active.windows} onSaved={mutate} />}
          </div>
        </div>
      )}

      {can("agents", "create") && <UploadForm onUploaded={mutate} />}

      {/* Version history */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-3 border-b border-gray-100 bg-gray-50 flex items-center gap-2">
          <DownloadCloud size={15} className="text-gray-600" />
          <p className="text-sm font-semibold text-gray-700">Version history</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[720px]">
          <thead>
            <tr className="text-gray-600 text-xs uppercase tracking-wide border-b border-gray-100">
              <th className="text-left px-4 py-2.5 font-semibold">Platform</th>
              <th className="text-left px-4 py-2.5 font-semibold">Version</th>
              <th className="text-left px-4 py-2.5 font-semibold">Type</th>
              <th className="text-left px-4 py-2.5 font-semibold">Size</th>
              <th className="text-left px-4 py-2.5 font-semibold">Status</th>
              <th className="text-left px-4 py-2.5 font-semibold">Distribution</th>
              <th className="px-4 py-2.5" />
            </tr>
          </thead>
          <tbody>
            {installers?.map(i => (
              <tr key={i.id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-4 py-3 capitalize text-gray-700">{i.platform}</td>
                <td className="px-4 py-3 font-medium text-gray-800">v{i.version}</td>
                <td className="px-4 py-3 text-gray-600 uppercase">{i.installer_type}</td>
                <td className="px-4 py-3 text-gray-600">{fmtBytes(i.file_size)}</td>
                <td className="px-4 py-3">
                  {i.is_active
                    ? <span className="inline-flex items-center gap-1 text-xs text-green-600"><CheckCircle2 size={13} /> Active</span>
                    : <span className="text-xs text-gray-600">inactive</span>}
                </td>
                <td className="px-4 py-3">
                  {i.is_active ? (
                    <span className="text-xs text-gray-600">
                      {i.rollout_percent ?? 100}% · {i.channel ?? "stable"}
                      {i.mandatory && <span className="ml-1.5 inline-flex items-center gap-0.5 text-amber-600"><AlertTriangle size={11} /> mandatory</span>}
                    </span>
                  ) : <span className="text-xs text-gray-500">—</span>}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  <button onClick={() => download(i.id)} className="text-gray-600 hover:text-brand-600 p-1"><Download size={15} /></button>
                  {can("agents", "edit") && !i.is_active && (
                    <button onClick={() => activate(i.id)} className="text-xs text-brand-600 hover:text-brand-700 ml-2">Activate</button>
                  )}
                  {can("agents", "delete") && (
                    <button onClick={() => remove(i.id, i.version)} title="Delete" className="text-gray-500 hover:text-red-500 p-1 ml-1 inline-flex">
                      <Trash2 size={14} />
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {installers?.length === 0 && <tr><td colSpan={7} className="px-4 py-10 text-center text-gray-600 text-sm">No agent builds uploaded yet.</td></tr>}
          </tbody>
        </table>
        </div>
      </div>

      {/* Fleet adoption + update status */}
      <FleetStatus />

      {/* Download history */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <div className="px-4 py-3 border-b border-gray-100 bg-gray-50 flex items-center gap-2">
          <History size={15} className="text-gray-600" />
          <p className="text-sm font-semibold text-gray-700">Download history</p>
          {downloads && downloads.length > 0 && (
            <span className="ml-auto text-xs text-gray-600">{downloads.length} record{downloads.length !== 1 ? "s" : ""}</span>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[640px]">
            <thead>
              <tr className="text-gray-600 text-xs uppercase tracking-wide border-b border-gray-100">
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
                        <p className="text-xs text-gray-600 truncate">{dl.downloaded_by_email}</p>
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
                    <span className="ml-1.5 text-xs text-gray-600 uppercase">{dl.installer_type}</span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-600 whitespace-nowrap">
                    {format(parseISO(dl.downloaded_at), "MMM d, yyyy · h:mm a")}
                  </td>
                </tr>
              ))}
              {downloads?.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-4 py-10 text-center text-gray-600 text-sm">
                    No downloads recorded yet.
                  </td>
                </tr>
              )}
              {!downloads && (
                <tr>
                  <td colSpan={4} className="px-4 py-6 text-center text-gray-500 text-sm animate-pulse">
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
